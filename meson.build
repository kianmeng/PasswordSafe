project('secrets',
        version: '7.0',
        meson_version: '>= 0.59',
        license: 'GPL-3.0-only'
       )
i18n = import('i18n')
python = import('python')
gnome = import('gnome')

project_id = 'org.gnome.World.Secrets'
project_name = 'Secrets'
package_url = 'https://gitlab.gnome.org/World/secrets'
copyright = '© 2018-2021 Falk Alexander Seidl, et al.'
contributors = '\n'.join([
    'Falk Alexander Seidl &lt;fseidl@gnome.org&gt;',
    'Uta Lemke',
    'David Heidelberg',
    'Jean Felder',
    'Maximiliano Sandoval &lt;msandova@gnome.org&gt;',
    'Sebastian Spaeth',
    'Christopher Davis',
                        ])
artists = '\n'.join([
'Christopher Davis',
'Tobias Bernard'
                   ])

message('Looking for dependencies')
# We need pykeepass>=4.0.3 and pyotp>=2.4.0.
python_bin = python.find_installation('python3', modules:[
  'pykeepass',
  'pyotp',
  'validators',
  'zxcvbn',
])
if not python_bin.found()
  error('No valid python3 binary found')
else
  message('Found python3 binary')
endif

if not python_bin.language_version().version_compare('>= 3.8')
  error('Python 3.8 or newer is required.')
endif


dependency('glib-2.0', version: '>= 2.66')
dependency('gio-2.0', version: '>= 2.66')
dependency('gobject-introspection-1.0', version: '>=1.66.0')
dependency('gtk4', version: '>=4.6.2')  # Needed for scroll-to-focus on Vierports
dependency('libadwaita-1', version: '>=1.2.alpha')

python_dir = python_bin.get_install_dir()
DATA_DIR = join_paths(get_option('prefix'), get_option('datadir'), meson.project_name())
bindir = join_paths(get_option('prefix'), get_option('bindir'))


# Profiles
application_id = project_id
if get_option('profile') == 'development'
    profile = 'Devel'
    application_id = '@0@.Devel'.format(application_id)
    version =  ''.join([run_command(['git', 'describe', '--long', '--tags']).stdout().strip()])
else
    profile = ''
    version = meson.project_version()
endif

top_source_dir = meson.current_source_dir()
gettext_package = meson.project_name()

subdir('data')
subdir('po')
subdir('gsecrets')

if get_option('tests')
    pytest = find_program(['pytest', 'pytest-3'], required : true)
    subdir('tests')
endif

gnome.post_install(
  gtk_update_icon_cache: true,
  glib_compile_schemas: true,
  update_desktop_database: true,
)
