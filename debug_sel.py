import subprocess
import time

import uiautomation as auto

TEXT = '桌面悬浮窗选区读取测试 12345 一二三四五'


def main():
    proc = subprocess.Popen(['notepad.exe'])
    try:
        time.sleep(2)
        w = auto.WindowControl(searchDepth=10, ClassName='Notepad')
        w.SetActive()
        time.sleep(0.5)
        auto.SetClipboardText(TEXT)
        win = auto.GetForegroundControl()
        auto.SendKeys('{Ctrl}v')
        time.sleep(0.5)
        auto.SendKeys('{Ctrl}a')
        time.sleep(0.3)
        auto.SendKeys('{Ctrl}c')
        time.sleep(0.3)
        got = auto.GetClipboardText()
        print('CLIPBOARD GOT len=%d [%s]' % (len(got), got[:60]))
        print('PASS' if got.strip() == TEXT else 'FAIL')
    finally:
        proc.terminate()


if __name__ == '__main__':
    main()