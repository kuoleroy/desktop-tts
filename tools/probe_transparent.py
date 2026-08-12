# -*- coding: utf-8 -*-
"""验证 events.loaded 是否触发 + native 是否可用"""
import os
import sys

os.chdir(r"E:\kuoleroy\desktop-tts")
sys.path.insert(0, os.getcwd())

import pet3d
srv, port = pet3d.start_server()

import webview

LOG = r"E:\kuoleroy\desktop-tts\web_req.log"


def log(s):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("[probe] " + s + "\n")


def on_loaded():
    log("loaded fired, native=" + repr(win.native))
    try:
        hwnd = win.native.Handle.ToInt32()
        log("hwnd=" + str(hwnd))
        pet3d.make_transparent(hwnd)
        log("make_transparent done")
    except Exception as e:
        log("ERR " + repr(e))


win = webview.create_window(
    "Probe",
    "http://127.0.0.1:%d/index.html" % port,
    width=240, height=300,
    transparent=True, frameless=True, on_top=True,
    easy_drag=False, js_api=pet3d.Api(),
)
win.events.loaded += on_loaded
webview.start()
log("start returned")