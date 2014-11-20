# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (nested_scopes, generators, division, absolute_import, with_statement,
                        print_function, unicode_literals)

import os
from pants.base.config import Config

from pants.backend.jvm.tasks.jvm_compile.analysis_tools import AnalysisTools
from pants.backend.jvm.tasks.jvm_compile.java.jmake_analysis import JMakeAnalysis
from pants.backend.jvm.tasks.jvm_compile.java.jmake_analysis_parser import JMakeAnalysisParser
from pants.backend.jvm.tasks.jvm_compile.jvm_compile import JvmCompile
from pants.base.build_environment import get_buildroot
from pants.base.exceptions import TaskError
from pants.base.target import Target
from pants.base.workunit import WorkUnit
from pants.util.dirutil import relativize_paths, safe_open


# From http://kenai.com/projects/jmake/sources/mercurial/content
#  /src/com/sun/tools/jmake/Main.java?rev=26
# Main.mainExternal docs.

_JMAKE_ERROR_CODES = {
   -1: 'invalid command line option detected',
   -2: 'error reading command file',
   -3: 'project database corrupted',
   -4: 'error initializing or calling the compiler',
   -5: 'compilation error',
   -6: 'error parsing a class file',
   -7: 'file not found',
   -8: 'I/O exception',
   -9: 'internal jmake exception',
  -10: 'deduced and actual class name mismatch',
  -11: 'invalid source file extension',
  -12: 'a class in a JAR is found dependent on a class with the .java source',
  -13: 'more than one entry for the same class is found in the project',
  -20: 'internal Java error (caused by java.lang.InternalError)',
  -30: 'internal Java error (caused by java.lang.RuntimeException).'
}
# When executed via a subprocess return codes will be treated as unsigned
_JMAKE_ERROR_CODES.update((256 + code, msg) for code, msg in _JMAKE_ERROR_CODES.items())


class JavaCompile(JvmCompile):
  _language = 'java'
  _file_suffix = '.java'
  _config_section = 'java-compile'

    # Well known metadata file to auto-register annotation processors with a java 1.6+ compiler
  _PROCESSOR_INFO_FILE = 'META-INF/services/javax.annotation.processing.Processor'

  _JMAKE_MAIN = 'com.sun.tools.jmake.Main'

  @classmethod
  def get_args_default(cls):
    return ('-C-encoding', '-CUTF-8', '-C-g', '-C-Tcolor',
            # Don't warn for generated code. Note that we assume that we're using the default
            # pants_workdir, which is always currently the case. In the future pants_workdir will
            # be a regular option, and we will be able to get its value here, default or otherwise.
            '-C-Tnowarnprefixes',
            '-C{0}'.format(os.path.join(Config.DEFAULT_PANTS_WORKDIR.default, 'gen')),
            # Suppress warning for annotations with no processor - we know there are many of these!
            '-C-Tnowarnregex', '-C^(warning: )?No processor claimed any of these annotations: .*')

  @classmethod
  def get_warning_args_default(cls):
    return ('-C-Xlint:all',   '-C-Xlint:-serial', '-C-Xlint:-path', '-C-deprecation')

  @classmethod
  def get_no_warning_args_default(cls):
    return ('-C-Xlint:none', '-C-nowarn')

  @classmethod
  def register_options(cls, register):
    super(JavaCompile, cls).register_options(register)
    register('--source', help='Provide source compatibility with this release.')
    register('--target', help='Generate class files for this JVM version.')

  def __init__(self, *args, **kwargs):
    super(JavaCompile, self).__init__(*args, **kwargs)
    self.set_distribution(jdk=True)

    self._buildroot = get_buildroot()

    self._depfile = os.path.join(self._analysis_dir, 'global_depfile')

    self._jmake_bootstrap_key = 'jmake'
    self.register_jvm_tool_from_config(self._jmake_bootstrap_key, self.context.config,
                                       ini_section='java-compile',
                                       ini_key='jmake-bootstrap-tools',
                                       default=['//:jmake'])

    self._compiler_bootstrap_key = 'java-compiler'
    self.register_jvm_tool_from_config(self._compiler_bootstrap_key, self.context.config,
                                       ini_section='java-compile',
                                       ini_key='compiler-bootstrap-tools',
                                       default=['//:java-compiler'])

  @property
  def config_section(self):
    return self._config_section

  def create_analysis_tools(self):
    return AnalysisTools(self.context, JMakeAnalysisParser(self._classes_dir), JMakeAnalysis)

  def extra_products(self, target):
    ret = []
    if target.is_apt and target.processors:
      root = os.path.join(self._resources_dir, Target.maybe_readable_identify([target]))
      processor_info_file = os.path.join(root, JavaCompile._PROCESSOR_INFO_FILE)
      self._write_processor_info(processor_info_file, target.processors)
      ret.append((root, [processor_info_file]))
    return ret

  # Make the java target language version part of the cache key hash,
  # this ensures we invalidate if someone builds against a different version.
  def platform_version_info(self):
    return (self.get_options().target,) if self.get_options().target else ()

  def compile(self, args, classpath, sources, classes_output_dir, analysis_file):
    relative_classpath = relativize_paths(classpath, self._buildroot)
    jmake_classpath = self.tool_classpath(self._jmake_bootstrap_key)
    args = [
      '-classpath', ':'.join(relative_classpath + [self._classes_dir]),
      '-d', self._classes_dir,
      '-pdb', analysis_file,
      '-pdb-text-format',
      ]

    compiler_classpath = self.tool_classpath(self._compiler_bootstrap_key)
    args.extend([
      '-jcpath', ':'.join(compiler_classpath),
      '-jcmainclass', 'com.twitter.common.tools.Compiler',
      ])

    if self.get_options().source:
      args.extend(['-C-source', '-C{0}'.format(self.get_options().source)])
    if self.get_options().target:
      args.extend(['-C-target', '-C{0}'.format(self.get_options().target)])

    if '-C-source' in self._args:
      raise TaskError("Set the source Java version with the 'source' option, not in 'args'.")
    if '-C-target' in self._args:
      raise TaskError("Set the target JVM version with the 'target' option, not in 'args'.")
    args.extend(self._args)

    args.extend(sources)
    result = self.runjava(classpath=jmake_classpath,
                          main=JavaCompile._JMAKE_MAIN,
                          jvm_options=self._jvm_options,
                          args=args,
                          workunit_name='jmake',
                          workunit_labels=[WorkUnit.COMPILER])
    if result:
      default_message = 'Unexpected error - JMake returned %d' % result
      raise TaskError(_JMAKE_ERROR_CODES.get(result, default_message))

  def post_process(self, relevant_targets):
    # Produce a monolithic apt processor service info file for further compilation rounds
    # and the unit test classpath.
    # This is distinct from the per-target ones we create in extra_products().
    all_processors = set()
    for target in relevant_targets:
      if target.is_apt and target.processors:
        all_processors.update(target.processors)
    processor_info_file = os.path.join(self._classes_dir, JavaCompile._PROCESSOR_INFO_FILE)
    if os.path.exists(processor_info_file):
      with safe_open(processor_info_file, 'r') as f:
        for processor in f:
          all_processors.add(processor)
    self._write_processor_info(processor_info_file, all_processors)

  def _write_processor_info(self, processor_info_file, processors):
    with safe_open(processor_info_file, 'w') as f:
      for processor in processors:
        f.write('%s\n' % processor.strip())

