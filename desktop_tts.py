import ctypes
import os
import json
import queue
import re
import sys
import tempfile
import threading
import time
import tkinter as tk

import requests
import uiautomation as auto

LOG = os.path.join(os.environ.get('TEMP', '.'), 'desktop_tts.log')


def log(msg):
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('%s %s\n' % (time.strftime('%H:%M:%S'), msg))
    except OSError:
        pass


def fg_process():
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return ''
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return ''
        try:
            buf = ctypes.create_unicode_buffer(1024)
            sz = ctypes.c_ulong(1024)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    h, 0, buf, ctypes.byref(sz)):
                return os.path.basename(buf.value).lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        pass
    return ''


SERVER = 'http://127.0.0.1:8848'
SERVER_EXE = r'D:\Programs\miniconda3\envs\cosyvoice\pythonw.exe'
SERVER_LAUNCHER = r'E:\kuoleroy\BrowserTTS\server\launcher.py'
CHUNK = 4000
GAP_S = 0.5
DEBOUNCE = 0.5
CONFIG = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')),
                      'desktop-tts', 'config.json')
def _load_voice_meta():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'voices.json'), encoding='utf-8') as f:
            data = json.load(f)
        voices = data.get('voices', {})
        sapi = data.get('sapi_voices', {})
        names = [c.get('name') for c in voices.values() if c.get('name')]
        ids = {c['name']: vid for vid, c in voices.items() if c.get('name')}
        s_names = [c.get('name') for c in sapi.values() if c.get('name')]
        s_ids = {c['name']: vid for vid, c in sapi.items() if c.get('name')}
        if names and s_names:
            return (names + s_names, dict(ids, **s_ids),
                    data.get('speed_list') or SPEED_LIST,
                    data.get('pitch_list') or PITCH_LIST)
    except Exception:
        pass
    return VOICE_NAMES, VOICE_IDS, SPEED_LIST, PITCH_LIST


SPEED_LIST = ['0.5', '0.7', '0.8', '0.9', '1.0', '1.2', '1.5', '2.0']
PITCH_LIST = ['-15Hz', '-10Hz', '-5Hz', '+0Hz', '+5Hz', '+10Hz', '+15Hz']
VOICE_NAMES = ['云健·男', '云扬·男', '云夏·男', '晓伊·女', '晓晓·女', '晓萱·女',
               '晓晓·本地', '云希·本地']
VOICE_IDS = {'云健·男': 'yunJian', '云扬·男': 'yunYang', '云夏·男': 'yunXia',
             '晓伊·女': 'xiaoYi', '晓晓·女': 'xiaoXiao', '晓萱·女': 'xiaoXuan',
             '晓晓·本地': 'xiaoXiaoLocal', '云希·本地': 'yunXiLocal'}
VOICE_NAMES, VOICE_IDS, SPEED_LIST, PITCH_LIST = _load_voice_meta()


class Player:
    def __init__(self):
        import pygame
        pygame.mixer.init()
        self.playing = False
        self.paused = False
        self._stop = threading.Event()

    def play(self, chunks):
        def _run():
            import pygame
            self._stop.clear()
            for idx, wav in enumerate(chunks):
                if self._stop.is_set():
                    break
                if idx > 0:
                    self._wait_if_paused()
                    if self._stop.is_set():
                        break
                    time.sleep(GAP_S)
                    self._wait_if_paused()
                if self._stop.is_set():
                    break
                pygame.mixer.music.load(wav)
                self.playing = True
                pygame.mixer.music.play()
                while not self._stop.is_set():
                    if self.paused:
                        time.sleep(0.1)
                        continue
                    if not pygame.mixer.music.get_busy():
                        break
                    time.sleep(0.1)
            self.playing = False
            pygame.mixer.music.stop()
            for wav in chunks:
                try:
                    os.remove(wav)
                except OSError:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def _wait_if_paused(self):
        while self.paused and not self._stop.is_set():
            time.sleep(0.1)

    def pause(self):
        import pygame
        if self.playing and not self.paused:
            pygame.mixer.music.pause()
            self.paused = True

    def resume(self):
        import pygame
        if self.playing and self.paused:
            pygame.mixer.music.unpause()
            self.paused = False

    def stop(self):
        self._stop.set()
        self.paused = False
        self.playing = False


class Grabber:
    def __init__(self, on_text, on_filter):
        self.on_text = on_text
        self.on_filter = on_filter
        self._last = 0.0
        self._last_text = ''
        self._token = 0

    def trigger(self, pos=None, delay=0.8):
        self._pos = pos
        self._proc = fg_process()
        self._token += 1
        token = self._token
        log('trigger fired pos=%s proc=%s delay=%.1fs' % (pos, self._proc, delay))
        threading.Thread(target=self._work, args=(token, delay), daemon=True).start()

    def _work(self, token, delay):
        if self._abort_wait(token, delay):
            return
        if not self.on_filter(self._proc):
            log('filtered out: %s' % self._proc)
            return
        time.sleep(0.15)
        text, _ = self._read_selection()
        text = ' '.join(text.split())[:20000]
        log('read %d chars: %s' % (len(text), text[:20].replace('\n', ' ')))
        if len(text) < 2 or text == self._last_text:
            log('rejected len<2=%s dup=%s' % (len(text) < 2, text == self._last_text))
            return
        if self.on_text(text, self._pos, self._proc):
            self._last_text = text

    def _abort_wait(self, token, delay):
        waited = 0.0
        while waited < delay:
            time.sleep(0.1)
            waited += 0.1
            if token != self._token:
                log('trigger cancelled by newer one')
                return True
        return False

    @staticmethod
    def _cursor_pos():
        try:
            import ctypes.wintypes
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return (pt.x, pt.y)
        except Exception:
            return None

    def _read_selection(self):
        text, rect = self._uia_selection()
        if text:
            return text, rect
        try:
            old = auto.GetClipboardText()
            auto.SendKeys('{Ctrl}c')
            time.sleep(0.25)
            text = auto.GetClipboardText()
            if text and text != old:
                auto.SetClipboardText(old)
                return text, None
        except Exception:
            pass
        return '', None

    def _uia_selection(self):
        try:
            win = auto.GetForegroundControl()
            todo = [(win, 0)]
            best = ''
            rect = None
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
                                rect = s.BoundingRectangle
                except Exception:
                    pass
                try:
                    tp = ctrl.GetTextPattern()
                    for r in tp.GetSelection():
                        t = r.GetText(-1)
                        if len(t) > len(best):
                            best = t
                            rect = r.GetBoundingRectangles()[0]
                except Exception:
                    pass
                for ch in ctrl.GetChildren():
                    todo.append((ch, depth + 1))
            return best, rect
        except Exception:
            return '', None


BG = '#1e1f24'
BG2 = '#26272e'
FG = '#e8eaed'
FG2 = '#9aa0aa'
BTN = '#3a3f4b'
BTN_HI = '#4a5060'
ACCENT = '#4f7cff'
FONT = ('Microsoft YaHei', 9)
FONT_B = ('Microsoft YaHei', 9, 'bold')


def _option_menu(parent, var, values, width=8):
    om = tk.OptionMenu(parent, var, *values)
    om.config(bg=BG2, fg=FG, activebackground=BTN_HI, activeforeground=FG,
              relief='flat', bd=0, highlightthickness=0, font=FONT,
              cursor='hand2', width=width)
    om['menu'].config(bg=BG2, fg=FG, activebackground=ACCENT,
                      activeforeground='white', bd=0, font=FONT)
    return om


def _button(parent, text, command, accent=False, disabled=False):
    return tk.Button(parent, text=text, command=command,
                     bg=ACCENT if accent else BTN,
                     activebackground=BTN_HI, fg='white',
                     relief='flat', bd=0, padx=9, pady=2, font=FONT,
                     cursor='hand2', state='disabled' if disabled else 'normal')


class App:
    def __init__(self):
        self.player = Player()
        self.win = tk.Tk()
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.configure(bg=BG)
        self._cur_scale = None
        self._scale_fonts = []
        self._build_ui()
        self.grabber = Grabber(self.on_text, self._on_filter)
        self._last_click = (0, 0, 0.0)
        self._drag_start = None
        self._hook_global()

    def _build_ui(self):
        self.ball_frm = tk.Frame(self.win, bg=BG)
        self.ball = tk.Label(self.ball_frm, text='读', width=4, height=2, bg=ACCENT,
                             fg='white', font=('Microsoft YaHei', 12, 'bold'),
                             cursor='hand2')
        self.ball.pack()
        self.ball.configure(cursor='hand2')

        self.panel_frm = tk.Frame(self.win, bg=BG, highlightbackground=BG2,
                                  highlightthickness=1)
        opts = tk.Frame(self.panel_frm, bg=BG)
        self._opts_frm = opts
        opts.pack(side='top', pady=(12, 6))
        self._opts_labels = []
        self._opts_menus = []
        self._buttons = []
        self.voice_var = tk.StringVar(value='云健·男')
        tk.Label(opts, text='音色', bg=BG, fg=FG2,
                 font=FONT).pack(side='left')
        tk.Label(opts, text='语速', bg=BG, fg=FG2,
                 font=FONT).pack(side='left')
        tk.Label(opts, text='语调', bg=BG, fg=FG2,
                 font=FONT).pack(side='left')
        for l in opts.winfo_children():
            self._opts_labels.append(l)
        self.speed_var = tk.StringVar(value='0.9')
        self.pitch_var = tk.StringVar(value='+0Hz')
        self._opts_menus = [None] * 3
        for i, (var, values, width) in enumerate((
                (self.voice_var, VOICE_NAMES, 8),
                (self.speed_var, SPEED_LIST, 5),
                (self.pitch_var, PITCH_LIST, 5))):
            self._opts_menus[i] = _option_menu(opts, var, values, width)
            self._opts_menus[i].pack(side='left', padx=2)
        self._load_config()
        row = tk.Frame(self.panel_frm, bg=BG)
        self._row_frm = row
        row.pack(side='top', pady=(6, 4))
        self.btn_read = _button(row, '朗读', self.read, accent=True)
        self.btn_pause = _button(row, '暂停', self.toggle_pause, disabled=True)
        self.btn_add_app = _button(row, '此软件', self._add_app)
        self.btn_export = _button(row, '导出MP3', self.export_mp3)
        self.btn_stop = _button(row, '停止', self.stop, disabled=True)
        self.btn_close = _button(row, '收起', self._show_ball_home)
        for b in row.winfo_children():
            self._buttons.append(b)
        self.btn_read.pack(side='left', padx=2)
        self.btn_pause.pack(side='left', padx=2)
        self.btn_add_app.pack(side='left', padx=2)
        self.btn_export.pack(side='left', padx=2)
        self.btn_stop.pack(side='left', padx=2)
        self.btn_close.pack(side='left', padx=2)
        self.lbl = tk.Label(self.panel_frm, text='', bg=BG, fg=FG2,
                            font=('Microsoft YaHei', 9), wraplength=340,
                            justify='left', height=6)
        self.lbl.pack(side='top', pady=(6, 2))
        self._resize_bar = tk.Label(self.panel_frm, text='▬', bg=BG2, fg=FG2,
                                    font=('Microsoft YaHei', 8), cursor='size_ns',
                                    pady=4)
        self._resize_bar.pack(side='bottom', fill='x')
        self._collect_scaleable(self.panel_frm)
        self._bind_drag(self.win)
        self.win.bind_all('<Button-3>', self._menu)

    def _bind_drag(self, w):
        for c in w.winfo_children():
            if isinstance(c, (tk.Button, tk.Menubutton, tk.OptionMenu)):
                continue
            for tag, cb in (('<ButtonPress-1>', self._on_win_press),
                            ('<B1-Motion>', self._on_win_drag),
                            ('<ButtonRelease-1>', self._on_win_release)):
                c.bind(tag, cb)
            self._bind_drag(c)
        self._no_grab_var = tk.BooleanVar(value=False)
        self._cache_var = tk.IntVar(value=500)
        self._filter_mode_var = tk.StringVar(value='all')
        self.filter_apps = []
        self._no_grab = False
        self._tray = None
        self._press_pos = None
        self._off = None
        self._last_ball_click = 0.0
        self._panel_h = None
        self._cur_scale = None
        self._scale_fonts = []
        self._last_proc = ''

    def _load_config(self):
        try:
            with open(CONFIG, encoding='utf-8') as f:
                cfg = json.load(f)
            if cfg.get('voice') in VOICE_NAMES:
                self.voice_var.set(cfg['voice'])
            if cfg.get('speed') in SPEED_LIST:
                self.speed_var.set(cfg['speed'])
            if cfg.get('pitch') in PITCH_LIST:
                self.pitch_var.set(cfg['pitch'])
            if isinstance(cfg.get('cache_limit'), int):
                self._cache_var.set(cfg['cache_limit'])
            if cfg.get('filter_mode') in ('all', 'whitelist', 'blacklist'):
                self._filter_mode_var.set(cfg['filter_mode'])
            if isinstance(cfg.get('filter_apps'), list):
                self.filter_apps = [a.lower() for a in cfg['filter_apps']]
        except Exception:
            pass

    def _save_config(self):
        try:
            os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
            with open(CONFIG, 'w', encoding='utf-8') as f:
                json.dump({'voice': self.voice_var.get(),
                           'speed': self.speed_var.get(),
                           'pitch': self.pitch_var.get(),
                           'cache_limit': self._cache_var.get(),
                           'filter_mode': self._filter_mode_var.get(),
                           'filter_apps': self.filter_apps},
                          f, ensure_ascii=False)
        except Exception:
            pass

    def _on_win_press(self, e):
        w, h = self.win.winfo_width(), self.win.winfo_height()
        bar = getattr(self, '_resize_bar', None)
        self._resizing = (w > 60 and self.panel_frm.winfo_ismapped()
                          and (e.widget is bar or e.y >= h - 16))
        self._resize_start = (w, h, e.x_root, e.y_root)
        self._press_pos = (e.x_root, e.y_root)
        self._off = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _on_win_drag(self, e):
        if self._resizing:
            w, h, sx, sy = self._resize_start
            self._panel_h = max(230, h + e.y_root - sy)
            self.win.geometry('%dx%d' % (w, self._panel_h))
            self._apply_scale(self._panel_h)
        elif self._off:
            self.win.geometry('+%d+%d' % (e.x_root - self._off[0],
                                          e.y_root - self._off[1]))

    def _on_win_release(self, e):
        moved = self._press_pos and (abs(e.x_root - self._press_pos[0]) > 5
                                     or abs(e.y_root - self._press_pos[1]) > 5)
        if not moved and not self._resizing:
            now = time.time()
            if now - self._last_ball_click < 0.35:
                self._last_ball_click = 0.0
                if self.ball_frm.winfo_ismapped():
                    self._show_panel()
                else:
                    self._show_ball()
            else:
                self._last_ball_click = now
        self._off = None
        self._resizing = False

    def _menu(self, e):
        m = tk.Menu(self.win, tearoff=0, bg=BG2, fg=FG,
                    activebackground=ACCENT, activeforeground='white', bd=0)
        m.add_command(label='收起', command=self._show_ball_home)
        m.add_command(label='最小化到任务栏', command=self._minimize)
        cache_menu = tk.Menu(m, tearoff=0, bg=BG2, fg=FG,
                             activebackground=ACCENT, activeforeground='white', bd=0)
        for label, val in (('100MB', 100), ('300MB', 300), ('500MB', 500),
                           ('1GB', 1024), ('不限制', 0)):
            cache_menu.add_radiobutton(label=label, command=self._save_config,
                                       variable=self._cache_var, value=val)
        cache_menu.add_separator()
        cache_menu.add_command(label='打开缓存文件夹', command=self._open_cache)
        m.add_cascade(label='缓存上限', menu=cache_menu)
        filter_menu = tk.Menu(m, tearoff=0, bg=BG2, fg=FG,
                              activebackground=ACCENT, activeforeground='white', bd=0)
        for label, val in (('全部软件', 'all'), ('仅以下软件', 'whitelist'),
                           ('排除以下软件', 'blacklist')):
            filter_menu.add_radiobutton(label=label, command=self._save_config,
                                        variable=self._filter_mode_var, value=val)
        filter_menu.add_separator()
        filter_menu.add_command(label='管理列表…', command=self._open_filter_mgr)
        m.add_cascade(label='朗读范围', menu=filter_menu)
        m.add_checkbutton(label='禁止识别', command=self._toggle_grab,
                          variable=self._no_grab_var)
        m.add_separator()
        m.add_command(label='退出', command=self._quit_confirm)
        m.tk_popup(e.x_root, e.y_root)

    def _open_cache(self):
        d = os.path.join(os.path.dirname(os.path.abspath(
            sys.executable if getattr(sys, 'frozen', False) else __file__)), 'cache')
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except Exception as e:
            log('open cache fail: %r' % e)

    def _toggle_grab(self):
        self._no_grab = self._no_grab_var.get()

    def _add_app(self):
        proc = self._last_proc or fg_process()
        if not proc or proc in ('pythonw.exe', 'python.exe', 'pythonservice.exe'):
            self.lbl.config(text='无法识别当前软件')
            return
        if proc not in self.filter_apps:
            self.filter_apps.append(proc)
            if self._filter_mode_var.get() == 'all':
                self._filter_mode_var.set('whitelist')
            self._save_config()
        mode = self._filter_mode_var.get()
        self.lbl.config(text='已加入: %s (%s)' % (
            proc, '仅此软件朗读' if mode == 'whitelist' else '排除'))

    def _on_filter(self, proc):
        mode = self._filter_mode_var.get()
        if mode == 'all':
            return True
        if mode == 'whitelist':
            return proc in self.filter_apps
        return proc not in self.filter_apps

    def _open_filter_mgr(self):
        w = tk.Toplevel(self.win)
        w.title('朗读范围')
        w.geometry('280x300')
        w.attributes('-topmost', True)
        lb = tk.Listbox(w, font=('Consolas', 10))
        lb.pack(fill='both', expand=True, padx=6, pady=6)
        for a in self.filter_apps:
            lb.insert('end', a)

        def remove():
            sel = lb.curselection()
            if sel:
                a = lb.get(sel[0])
                if a in self.filter_apps:
                    self.filter_apps.remove(a)
                lb.delete(sel[0])
                self._save_config()

        def start_add():
            lb.configure(state='disabled')
            w.withdraw()
            w.after(100, lambda: poll_add(0))

        def poll_add(tries):
            p = fg_process()
            if p and p not in ('pythonw.exe', 'python.exe', 'pythonservice.exe'):
                if p not in self.filter_apps:
                    self.filter_apps.append(p)
                    lb.insert('end', p)
                    self._save_config()
                lb.configure(state='normal')
                w.deiconify()
                return
            if tries < 60:
                w.after(150, lambda: poll_add(tries + 1))
            else:
                lb.configure(state='normal')
                w.deiconify()

        row = tk.Frame(w)
        row.pack(side='bottom', fill='x', padx=6, pady=6)
        tk.Button(row, text='添加当前软件…', command=start_add,
                  bg='#2f6fed', fg='white', relief='flat').pack(side='left')
        tk.Button(row, text='删除选中', command=remove,
                  bg='#cc4444', fg='white', relief='flat').pack(side='left', padx=6)

    def _minimize(self):
        self.win.withdraw()
        if self._tray is not None:
            return
        try:
            import pystray
            from PIL import Image, ImageDraw
            img = Image.new('RGB', (64, 64), (240, 248, 255))
            d = ImageDraw.Draw(img)
            d.ellipse((6, 6, 58, 58), fill=(70, 130, 240), outline=(30, 60, 140), width=3)
            menu = pystray.Menu(
                pystray.MenuItem('显示窗口', self._tray_show, default=True),
                pystray.MenuItem('退出', self._tray_quit))
            self._tray = pystray.Icon('desktop-tts', img, '桌面朗读助手', menu)
            threading.Thread(target=self._tray.run, daemon=True).start()
            log('minimized to tray')
        except Exception as e:
            log('tray fail: %r' % e)
            self.win.deiconify()

    def _tray_show(self, icon=None, item=None):
        self.win.after(0, self._show_ball_home)

    def _tray_quit(self, icon=None, item=None):
        self.win.after(0, self.win.destroy)

    def _quit_confirm(self):
        from tkinter import messagebox
        if messagebox.askokcancel('退出朗读助手', '确定要退出吗？', parent=self.win):
            self.win.destroy()

    def _clamp_pos(self, x, y, w, h):
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x = max(0, min(x, sw - w - 8))
        y = max(0, min(y, sh - h - 8))
        return x, y

    def _show_ball(self):
        self._last_ball_click = 0.0
        self.win.deiconify()
        self.grabber._last_text = ''
        self.panel_frm.pack_forget()
        self.ball_frm.pack(fill='both', expand=True)
        w, h = self.win.winfo_width(), self.win.winfo_height()
        x, y = self.win.winfo_x(), self.win.winfo_y()
        if w < 2 or h < 2:
            w, h = 70, 70
        x, y = self._clamp_pos(x, y, w, h)
        self.win.geometry('70x70+%d+%d' % (x, y))

    def _show_ball_home(self):
        self._panel_h = None
        self._show_ball()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        self.win.geometry('70x70+%d+%d' % (sw - 100, sh - 140))

    def _show_panel(self):
        self.ball_frm.pack_forget()
        self.panel_frm.pack(fill='both', expand=True)
        self.win.geometry('')
        self.win.update_idletasks()
        h = max(self.win.winfo_height(), self._panel_h or 0, 230)
        self.win.geometry('%dx%d' % (self.win.winfo_width(), h))
        self.win.update_idletasks()
        self._apply_scale()
        x, y = self.win.winfo_x(), self.win.winfo_y()
        x, y = self._clamp_pos(x, y, self.win.winfo_width(), self.win.winfo_height())
        self.win.geometry('+%d+%d' % (x, y))

    def _apply_scale(self, h=None):
        h = h or self.win.winfo_height()
        size = 9 + max(0, (h - 230) // 110)
        if size == self._cur_scale:
            return
        self._cur_scale = size
        for w, fam, base in self._scale_fonts:
            if w is self.lbl:
                continue
            w.config(font=(fam, base + max(0, size - 9)))
        self.lbl.config(font=('Microsoft YaHei', max(8, 9 + size - 9 - 1)),
                        wraplength=max(200, self.win.winfo_width() - 22))

    def _collect_scaleable(self, w):
        for c in w.winfo_children():
            if isinstance(c, (tk.Button, tk.Menubutton, tk.OptionMenu, tk.Label)):
                f = c.cget('font')
                if isinstance(f, tuple) and len(f) > 1:
                    fam, base = f[0], f[1]
                else:
                    fam, base = 'Microsoft YaHei', 9
                self._scale_fonts.append((c, fam, base))
            self._collect_scaleable(c)

    def _hook_global(self):
        try:
            from pynput import mouse, keyboard
            self._mlistener = mouse.Listener(on_click=self._on_click)
            self._mlistener.start()
            self._ctrl_down = False
            self._klistener = keyboard.Listener(on_press=self._on_key,
                                                on_release=self._on_key_up)
            self._klistener.start()
        except Exception as e:
            print('hook failed:', e)

    def _on_click(self, x, y, button, pressed):
        try:
            if button.name == 'left':
                now = time.time()
                if pressed:
                    lx, ly, lt = self._last_click
                    if now - lt < 0.35 and abs(x - lx) < 6 and abs(y - ly) < 6:
                        self._last_click = (x, y, 0.0)
                        log('dbl-click trigger at (%d,%d)' % (x, y))
                        self.grabber.trigger((x, y))
                    else:
                        self._last_click = (x, y, now)
                    self._drag_start = (x, y)
                elif self._drag_start:
                    sx, sy = self._drag_start
                    self._drag_start = None
                    if max(abs(x - sx), abs(y - sy)) >= 8:
                        log('drag trigger from (%d,%d) to (%d,%d)' % (sx, sy, x, y))
                        self.grabber.trigger((x, y))
        except Exception:
            pass

    def _on_key(self, key):
        try:
            from pynput import keyboard as kb
            if key == kb.Key.esc:
                self.win.after(0, self._show_ball_home)
                return
            if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
                self._ctrl_down = True
            elif self._ctrl_down and getattr(key, 'vk', None) == 65:
                log('ctrl+a trigger')
                self.grabber.trigger(None)
        except Exception:
            pass

    def _on_key_up(self, key):
        try:
            from pynput import keyboard as kb
            if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
                self._ctrl_down = False
        except Exception:
            pass

    def on_text(self, text, pos=None, proc=''):
        if self._no_grab:
            return False
        self._last_proc = proc
        self.win.after(0, self._show, text, pos)
        return True

    def _show(self, text, pos=None):
        self.cur_text = text
        self._show_panel()
        self.win.update_idletasks()
        self.lbl.config(text='已选 %d 字 · %s' % (len(text), '全文' if len(text) > 500 else '选区'))
        self.btn_read.config(state='normal')
        self.btn_stop.config(state='normal')
        self.btn_pause.config(state='normal')
        if pos:
            mx, my = pos
        else:
            mx, my = self.win.winfo_pointerx(), self.win.winfo_pointery()
        self.win.update_idletasks()
        x = self.win.winfo_rootx() + mx - (self.btn_read.winfo_rootx()
                                          + self.btn_read.winfo_width() // 2)
        y = self.win.winfo_rooty() + my - (self.btn_read.winfo_rooty()
                                           + self.btn_read.winfo_height() // 2)
        x, y = self._clamp_pos(x, y, self.win.winfo_width(), self.win.winfo_height())
        self.win.geometry('+%d+%d' % (x, y))
        log('panel at (%d,%d) size %dx%d mouse was (%d,%d)'
            % (x, y, self.win.winfo_width(), self.win.winfo_height(),
               self.win.winfo_pointerx(), self.win.winfo_pointery()))

    def ensure_server(self):
        log('ensure_server start')
        try:
            requests.get(SERVER + '/health', timeout=1)
            log('ensure_server already up')
            return True
        except Exception as e:
            log('health check fail: %s' % e)
        try:
            import uvicorn
            from server_core import app as srv_app
            log('starting embedded uvicorn')
            cfg = uvicorn.Config(srv_app, host='127.0.0.1', port=8848,
                                 log_level='warning', log_config=None)
            self._srv = uvicorn.Server(cfg)
            threading.Thread(target=self._srv.run, daemon=True).start()
            for i in range(40):
                time.sleep(1)
                try:
                    requests.get(SERVER + '/health', timeout=1)
                    log('embedded uvicorn up after %ds' % (i + 1))
                    return True
                except Exception:
                    pass
            log('embedded uvicorn never became ready')
        except Exception as e:
            log('ensure_server error: %r' % e)
        return False

    def read(self):
        if not hasattr(self, 'cur_text') or not self.cur_text:
            return
        self.player.stop()
        if not self.ensure_server():
            self.lbl.config(text='服务启动失败')
            return
        text = self.cur_text
        self._save_config()
        self.lbl.config(text='合成中…')
        threading.Thread(target=self._read_worker, args=(text,), daemon=True).start()

    def _synthesize(self, text):
        text = re.sub(r'[*#|~^_`{}\[\]\\]', '', text)
        chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
        voice = VOICE_IDS[self.voice_var.get()]
        speed = float(self.speed_var.get())
        pitch = self.pitch_var.get()
        files = []
        for c in chunks:
            r = requests.post(SERVER + '/tts',
                              json={'text': c, 'speed': speed, 'voice': voice,
                                    'pitch': pitch},
                              timeout=600)
            r.raise_for_status()
            ext = 'wav' if r.headers.get('content-type', '').endswith('wav') else 'mp3'
            tmp = os.path.join(tempfile.gettempdir(), 'dtts_%d.%s' % (time.time_ns(), ext))
            with open(tmp, 'wb') as f:
                f.write(r.content)
            files.append(tmp)
        return files

    def _read_worker(self, text):
        try:
            wavs = self._synthesize(text)
        except Exception as e:
            self.win.after(0, lambda: self.lbl.config(text='失败: %s' % e))
            return
        self.win.after(0, self._begin_play, text, wavs)

    def _begin_play(self, text, wavs):
        self._start_scroll(text)
        self.player.play(wavs)

    def _start_scroll(self, text):
        self._scroll_text = ' '.join(text.split())
        self._scroll_pos = 0
        self._scroll_n = None
        self._tick_scroll()

    def _tick_scroll(self):
        if not (self.player.playing or self.player.paused):
            self._scroll_job = None
            return
        n = max(4, (self.win.winfo_width() - 22) // max(8, self._cur_scale or 9))
        if n != self._scroll_n:
            self._scroll_n = n
            self._scroll_lines = [self._scroll_text[i:i + n]
                                  for i in range(0, len(self._scroll_text), n)]
        lines = self._scroll_lines
        if len(lines) <= 6:
            self.lbl.config(text='\n'.join(lines))
            self._scroll_job = None
            return
        self._scroll_pos = min(self._scroll_pos, len(lines) - 6)
        self.lbl.config(text='\n'.join(lines[self._scroll_pos:self._scroll_pos + 6]))
        self._scroll_pos = (self._scroll_pos + 1) % (len(lines) - 5)
        self._scroll_job = self.win.after(300, self._tick_scroll)

    def export_mp3(self):
        if not hasattr(self, 'cur_text') or not self.cur_text:
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            parent=self.win, title='导出 MP3', defaultextension='.mp3',
            initialfile='朗读_%s.mp3' % time.strftime('%Y%m%d_%H%M%S'),
            filetypes=[('MP3 音频', '*.mp3')])
        if not path:
            return
        if not self.ensure_server():
            self.lbl.config(text='服务启动失败')
            return
        self._save_config()
        self.lbl.config(text='导出中…')
        threading.Thread(target=self._export_worker, args=(self.cur_text, path),
                         daemon=True).start()

    def _export_worker(self, text, path):
        try:
            files = self._synthesize(text)
            import shutil, subprocess
            ff = shutil.which('ffmpeg')
            parts = []
            for f in files:
                if f.endswith('.mp3'):
                    parts.append(f)
                elif ff:
                    out = f[:-4] + '.mp3'
                    subprocess.run([ff, '-y', '-i', f, '-b:a', '192k', out],
                                   capture_output=True)
                    parts.append(out)
            if len(parts) != len(files):
                self.win.after(0, lambda: self.lbl.config(
                    text='含长文本段，需要 ffmpeg 转码'))
                return
            with open(path, 'wb') as out:
                for p in parts:
                    with open(p, 'rb') as src:
                        out.write(src.read())
        except Exception as e:
            self.win.after(0, lambda: self.lbl.config(text='导出失败: %s' % e))
            return
        self.win.after(0, lambda: self.lbl.config(
            text='已导出 %s' % os.path.basename(path)))

    def toggle_pause(self):
        if self.player.paused:
            self.player.resume()
            self.btn_pause.config(text='暂停')
        elif self.player.playing:
            self.player.pause()
            self.btn_pause.config(text='继续')

    def stop(self):
        self.player.stop()
        self.btn_pause.config(text='暂停', state='normal')
        self.lbl.config(text='已停止')

    def run(self):
        self._show_ball_home()
        self.win.mainloop()


if __name__ == '__main__':
    App().run()