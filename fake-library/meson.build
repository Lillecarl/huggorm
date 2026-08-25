project('fake-library', 'cpp',
  version : '0.1.0',
  default_options : ['cpp_std=c++23', 'warning_level=2']
)

inc = include_directories('include')

# Required, not optional: gc-env.hpp has no no-GC path, and every
# consumer TU must define the same flag or the ODR agreement breaks.
gc_dep = dependency('bdw-gc', required: true)

lib = library('fake_library',
  'src/store.cpp',
  'src/eval.cpp',
  include_directories : inc,
  cpp_args : ['-DFAKE_LIBRARY_USE_BOEHMGC=1'],
  dependencies : [gc_dep],
  install : true,
)

# For pkg-config / dependency() use in bindings if we want later
pkg = import('pkgconfig')
pkg.generate(lib,
  name : 'fake_library',
  description : 'Mock of the Nix store domain for the Cython wrapper experiment',
)

install_subdir('include/fake_library', install_dir : get_option('includedir'))
