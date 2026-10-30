pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()
moduledir = pkgdatadir / 'thirties'
gnome = import('gnome')

gnome.compile_resources('thirties',
  'thirties.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)

python = import('python')

conf = configuration_data()
conf.set('PYTHON', python.find_installation('python3').full_path())
conf.set('VERSION', meson.project_version())
conf.set('localedir', get_option('prefix') / get_option('localedir'))
conf.set('pkgdatadir', pkgdatadir)

configure_file(
  input: 'thirties.in',
  output: 'thirties',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x'
)

vcs_tag(
  command: ['git', '-C', meson.project_source_root(), 'describe', '--always', '--dirty'],
  input: 'build_info.py.in',
  output: 'build_info.py',
  fallback: 'unknown',
  install: true,
  install_dir: moduledir,
)

thirties_sources = [
  '__init__.py',
  'main.py',
  'portal.py',
  'window.py',
]

install_data(thirties_sources, install_dir: moduledir)
install_subdir('views', install_dir: moduledir)
