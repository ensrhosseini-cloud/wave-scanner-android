[app]
title = WaveScanner Futures
package.name = wavescanner
package.domain = org.wavescanner
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,ttf,txt
source.exclude_dirs = tests,.git,.github,.venv,__pycache__,bin
version = 2.0.0
requirements = python3,kivy==2.3.1,numpy,pandas,requests,certifi,arabic-reshaper,python-bidi==0.4.2,pyjnius
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,ACCESS_NETWORK_STATE
android.api = 35
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a
android.accept_sdk_license = True
android.private_storage = True

[buildozer]
log_level = 2
warn_on_root = 1
