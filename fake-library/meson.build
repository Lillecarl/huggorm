project('fake-library', 'cpp',
  version : '0.1.0',
  default_options : ['cpp_std=c++17', 'warning_level=2']
)

inc = include_directories('include')

lib = library('fake_library',
  'src/animal.cpp',
  include_directories : inc,
  install : true,
)

# For pkg-config / dependency() use in bindings if we want later
pkg = import('pkgconfig')
pkg.generate(lib,
  name : 'fake_library',
  description : 'Fake C++ library for Cython demo',
)

install_subdir('include/fake_library', install_dir : get_option('includedir'))
