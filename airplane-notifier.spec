# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec (U11 -- R3, R17, R18).

--onedir, not --onefile: onefile unpacks to a temp directory on every launch,
which Windows SmartScreen treats as suspicious. A stable folder with a visible
exe alongside its DLLs is handled far better (KTD5).

`credentials.json` is deliberately NOT bundled. Baking it in would publish the
OAuth client secret to anyone holding a copy of the build (plan review item 7).
The app looks for it next to the exe instead, so it is dropped in after the
build and can be replaced without rebuilding.

Builds TWO executables into one shared dist folder: the main app, and a
watchdog that relaunches it if it ever stops running (crash, kill, a login
launch that silently failed). The watchdog gets its own `Analysis` rather
than reusing the main one -- its whole dependency graph is stdlib plus this
package's own log/paths modules, neither of which imports PyQt6 or the
Google client, so analysing it separately keeps it from dragging ~100 MB of
unrelated libraries into a process whose only job is watching a mutex.
"""

block_cipher = None

a_main = Analysis(
    ['airplanenotifier/__main__.py'],
    pathex=[],
    binaries=[],
    # The whole assets directory, so adding art needs no spec change.
    datas=[('assets', 'assets')],
    hiddenimports=[
        # Both are reached dynamically, so PyInstaller's static analysis
        # cannot see them.
        'google_auth_oauthlib.flow',
        'googleapiclient.discovery',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Pillow is a build-time tool for generating assets, never a runtime
        # dependency. Excluding it keeps a few MB out of the bundle.
        'PIL',
        'pytest',
        'tkinter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

def _keep_datum(entry):
    """Drop google-api-python-client's discovery documents except Calendar's.

    The library ships a static discovery JSON for every Google API -- 586 of
    them, about 100 MB, versus the ~1 MB we need. Keeping calendar.v3.json
    preserves static discovery, so build() still works without an extra network
    round trip at startup.
    """
    destination = entry[0].replace('\\', '/').lower()
    if 'discovery_cache/documents/' not in destination:
        return True
    return destination.endswith('/calendar.v3.json')


a_main.datas = [entry for entry in a_main.datas if _keep_datum(entry)]

a_watchdog = Analysis(
    ['airplanenotifier/watchdog.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # None of these are ever imported by watchdog.py or its own imports
        # (log.py, paths.py); excluding them is belt-and-braces against
        # PyInstaller's hooks pulling in more than the script actually needs.
        'PIL',
        'pytest',
        'tkinter',
        'PyQt6',
        'google',
        'googleapiclient',
        'google_auth_oauthlib',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz_main = PYZ(a_main.pure, a_main.zipped_data, cipher=block_cipher)

exe_main = EXE(
    pyz_main,
    a_main.scripts,
    [],
    exclude_binaries=True,
    name='airplane-notifier',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # A tray app has no console; a stray black window on every login is worse
    # than losing stderr, which the app only uses for diagnostics anyway.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

pyz_watchdog = PYZ(a_watchdog.pure, a_watchdog.zipped_data, cipher=block_cipher)

exe_watchdog = EXE(
    pyz_watchdog,
    a_watchdog.scripts,
    [],
    exclude_binaries=True,
    name='airplane-notifier-watchdog',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe_main,
    a_main.binaries,
    a_main.zipfiles,
    a_main.datas,
    exe_watchdog,
    a_watchdog.binaries,
    a_watchdog.zipfiles,
    a_watchdog.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='airplane-notifier',
)
