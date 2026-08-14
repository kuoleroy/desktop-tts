# -*- coding: utf-8 -*-
"""两步验证 UIA 事件管道：
Part A: 自动打开记事本 → 应收到「窗口打开事件」(20016)，验证 comtypes 事件管道本身可用。
Part B: 请手动在记事本/浏览器里拖选文字 → 应收到「选择变化事件」(20014)。
无鼠标钩子、无全局监控。
"""
import ctypes
import ctypes.wintypes
import subprocess
import sys
import time

COINIT_APARTMENTTHREADED = 0x2

def dbg(m):
    sys.stderr.write("[probe] " + m + "\n")
    sys.stderr.flush()

ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)

import comtypes
import comtypes.client
comtypes.client.GetModule(r"C:\Windows\System32\UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as UIA
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
from ctypes import POINTER, c_int

uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
root = uia.GetRootElement()

class IUIAutomationEventHandler(IUnknown):
    _iid_ = GUID("{146c3c17-f12e-4e22-8c27-f894b9b79c69}")
    _methods_ = [
        COMMETHOD([], HRESULT, 'HandleAutomationEvent',
                  (['in'], POINTER(IUnknown), 'sender'),
                  (['in'], c_int, 'eventId')),
    ]

handler_calls = []
class MyHandler(comtypes.COMObject):
    _com_interfaces_ = [IUIAutomationEventHandler]
    def HandleAutomationEvent(self, sender, eventId):
        handler_calls.append(eventId)
        dbg("EVENT id=%s (total %d)" % (eventId, len(handler_calls)))

h = MyHandler()  # 强引用，防止被 GC
uia.AddAutomationEventHandler(20014, root, 4, None, h)  # 选择变化
uia.AddAutomationEventHandler(20016, root, 4, None, h)  # 窗口打开
dbg("events registered (20014 selection, 20016 window-opened)")

# Part A: 自动打开记事本触发窗口打开事件
subprocess.Popen(["notepad.exe"], close_fds=True)
dbg(">>> 已启动记事本，等待窗口打开事件 (Part A) <<<")

msg = ctypes.wintypes.MSG()
deadline = 1200  # 60s
stage = "A"
while deadline > 0:
    while ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
        ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
        ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
    if stage == "A" and 20016 in handler_calls:
        dbg("PART A PASS: 收到窗口打开事件")
        stage = "B"
        dbg(">>> 请在记事本或浏览器里用鼠标拖选一段文字 (Part B) <<<")
    if stage == "B" and 20014 in handler_calls:
        break
    time.sleep(0.05)
    deadline -= 1

dbg("events received: %s" % handler_calls)
if 20016 in handler_calls:
    dbg("PROBE RESULT A: PASS (窗口打开事件管道可用)")
else:
    dbg("PROBE RESULT A: FAIL (事件管道可能有问题)")
if 20014 in handler_calls:
    dbg("PROBE RESULT B: PASS (选择变化事件可用)")
else:
    dbg("PROBE RESULT B: NO (60s 内未收到选择事件，或所选应用不发该事件)")
