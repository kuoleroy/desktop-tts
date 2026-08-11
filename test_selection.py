import subprocess
import time

import uiautomation as auto

TEXT = '桌面悬浮窗选区读取测试 12345 一二三四五'


def select_all():
    w = auto.WindowControl(searchDepth=10, ClassName='Notepad')
    w.SetActive()
    edit = w.EditControl(searchDepth=5)
    edit.GetPattern(auto.PatternId.SelectionPattern).Select(edit.GetSelectionPattern().GetCurrentSelection()[0])
    time.sleep(0.3)
    edit.SetFocus()
    edit.SendKeys('^a')
    time.sleep(0.3)


def main():
    proc = subprocess.Popen(['notepad.exe'])
    try:
        time.sleep(2)
        w = auto.WindowControl(searchDepth=10, ClassName='Notepad')
        w.SetActive()
        edit = w.EditControl(searchDepth=5)
        edit.GetValuePattern().SetValue(TEXT)
        edit.SendKeys('^a')
        time.sleep(0.5)
        win = auto.GetForegroundControl()
        best = ''
        todo = [(win, 0)]
        while todo:
            ctrl, depth = todo.pop(0)
            if depth > 10:
                continue
            try:
                sp = ctrl.GetSelectionPattern()
                if sp:
                    for s in sp.GetCurrentSelection():
                        t = s.Name
                        if len(t) > len(best):
                            best = t
            except Exception:
                pass
            for ch in ctrl.GetChildren():
                todo.append((ch, depth + 1))
        print('SELECTED TEXT: [%s]' % best)
        print('PASS' if best == TEXT else 'FAIL')
    finally:
        proc.terminate()


if __name__ == '__main__':
    main()