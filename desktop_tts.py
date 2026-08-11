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

SERVER = 'http://127.0.0.1:8848'
SERVER_EXE = r'D:\Programs\miniconda3\envs\cosyvoice\pythonw.exe'
SERVER_LAUNCHER = r'E:\kuoleroy\BrowserTTS\server\launcher.py'
CHUNK = 4000
GAP_S = 0.5
DEBOUNCE = 0.5
CONFIG = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')),
                      'desktop-tts', 'config.json')
VOICE_NAMES = ['云健·男', '云扬·男', '云夏·男', '晓伊·女', '晓晓·女', '晓萱·女']
VOICE_IDS = {'云健·男': 'yunJian', '云扬·男': 'yunYang', '云夏·男': 'yunXia',
             '晓伊·女': 'xiaoYi', '晓晓·女': 'xiaoXiao', '晓萱·女': 'xiaoXuan'}
SPEED_LIST = ['0.5', '0.7', '0.8', '0.9', '1.0', '1.2', '1.5', '2.0']
PITCH_LIST = ['-15Hz', '-10Hz', '-5Hz', '+0Hz', '+5Hz', '+10Hz', '+15Hz']


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
    def __init__(self, on_text):
        self.on_text = on_text
        self._last = 0.0
        self._last_text = ''

    def trigger(self):
        now = time.time()
        if now - self._last < DEBOUNCE:
            return
        self._last = now
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        time.sleep(0.25)
        text = self._read_selection()
        text = ' '.join(text.split())[:20000]
        if len(text) < 2 or text == self._last_text:
            return
        self._last_text = text
        self.on_text(text)

    def _read_selection(self):
        text = self._uia_selection()
        if text:
            return text
        try:
            old = auto.GetClipboardText()
            auto.SendKeys('{Ctrl}c')
            time.sleep(0.25)
            text = auto.GetClipboardText()
            if text and text != old:
                auto.SetClipboardText(old)
                return text
        except Exception:
            pass
        return ''

    def _uia_selection(self):
        try:
            win = auto.GetForegroundControl()
            todo = [(win, 0)]
            best = ''
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
                try:
                    tp = ctrl.GetTextPattern()
                    for r in tp.GetSelection():
                        t = r.GetText(-1)
                        if len(t) > len(best):
                            best = t
                except Exception:
                    pass
                for ch in ctrl.GetChildren():
                    todo.append((ch, depth + 1))
            return best
        except Exception:
            return ''


class App:
    def __init__(self):
        self.player = Player()
        self.win = tk.Tk()
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.configure(bg='#2f6fed')
        self._build_ui()
        self.grabber = Grabber(self.on_text)
        self._last_click = (0, 0, 0.0)
        self._drag_start = None
        self._hook_global()

    def _build_ui(self):
        self.ball_frm = tk.Frame(self.win, bg='#2f6fed')
        self.ball = tk.Label(self.ball_frm, text='读', width=4, height=2, bg='#2f6fed',
                             fg='white', font=('Microsoft YaHei', 12, 'bold'),
                             cursor='hand2')
        self.ball.pack()
        self.ball.bind('<ButtonPress-1>', self._press_ball)
        self.ball.bind('<B1-Motion>', self._drag_ball)
        self.ball.bind('<ButtonRelease-1>', self._release_ball)
        self.ball.bind('<Button-3>', lambda e: self._menu(e))

        self.panel_frm = tk.Frame(self.win, bg='#2f6fed')
        opts = tk.Frame(self.panel_frm, bg='#2f6fed')
        opts.pack(side='top', pady=(0, 2))
        self.voice_var = tk.StringVar(value='云健·男')
        tk.Label(opts, text='音色', bg='#2f6fed', fg='white',
                 font=('Microsoft YaHei', 9)).pack(side='left')
        tk.OptionMenu(opts, self.voice_var, *VOICE_NAMES).pack(side='left', padx=2)
        tk.Label(opts, text='语速', bg='#2f6fed', fg='white',
                 font=('Microsoft YaHei', 9)).pack(side='left')
        self.speed_var = tk.StringVar(value='0.9')
        tk.OptionMenu(opts, self.speed_var, *SPEED_LIST).pack(side='left', padx=2)
        tk.Label(opts, text='语调', bg='#2f6fed', fg='white',
                 font=('Microsoft YaHei', 9)).pack(side='left')
        self.pitch_var = tk.StringVar(value='+0Hz')
        tk.OptionMenu(opts, self.pitch_var, *PITCH_LIST).pack(side='left', padx=2)
        self._load_config()
        self.lbl = tk.Label(self.panel_frm, text='', bg='#2f6fed', fg='white',
                            font=('Microsoft YaHei', 10))
        self.lbl.pack(side='top')
        row = tk.Frame(self.panel_frm, bg='#2f6fed')
        row.pack(side='top', pady=(4, 0))
        self.btn_read = tk.Button(row, text='朗读', command=self.read,
                                  bg='#1d4fc0', fg='white', relief='flat', padx=10)
        self.btn_pause = tk.Button(row, text='暂停', command=self.toggle_pause,
                                   bg='#1d4fc0', fg='white', relief='flat', padx=8, state='disabled')
        self.btn_stop = tk.Button(row, text='停止', command=self.stop,
                                  bg='#1d4fc0', fg='white', relief='flat', padx=8, state='disabled')
        self.btn_close = tk.Button(row, text='收起', command=self._show_ball,
                                   bg='#1d4fc0', fg='white', relief='flat', padx=8)
        self.btn_read.pack(side='left', padx=2)
        self.btn_pause.pack(side='left', padx=2)
        self.btn_stop.pack(side='left', padx=2)
        self.btn_close.pack(side='left', padx=2)
        self._press_pos = None

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
        except Exception:
            pass

    def _save_config(self):
        try:
            os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
            with open(CONFIG, 'w', encoding='utf-8') as f:
                json.dump({'voice': self.voice_var.get(),
                           'speed': self.speed_var.get(),
                           'pitch': self.pitch_var.get()},
                          f, ensure_ascii=False)
        except Exception:
            pass

    def _press_ball(self, e):
        self._press_pos = (e.x_root, e.y_root)
        self._offx = e.x
        self._offy = e.y

    def _drag_ball(self, e):
        self.win.geometry('+%d+%d' % (e.x_root - self._offx, e.y_root - self._offy))

    def _release_ball(self, e):
        if self._press_pos and abs(e.x_root - self._press_pos[0]) < 5 \
                and abs(e.y_root - self._press_pos[1]) < 5:
            self._show_panel()

    def _menu(self, e):
        m = tk.Menu(self.win, tearoff=0)
        m.add_command(label='退出', command=self.win.destroy)
        m.tk_popup(e.x_root, e.y_root)

    def _show_ball(self):
        self.panel_frm.pack_forget()
        self.ball_frm.pack()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry('70x70+%d+%d' % (sw - 100, sh - 140))

    def _show_panel(self):
        self.ball_frm.pack_forget()
        self.panel_frm.pack(padx=6, pady=4)
        self.win.geometry('')

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
                        self.grabber.trigger()
                    else:
                        self._last_click = (x, y, now)
                    self._drag_start = (x, y)
                elif self._drag_start:
                    sx, sy = self._drag_start
                    self._drag_start = None
                    if max(abs(x - sx), abs(y - sy)) >= 8:
                        self.grabber.trigger()
        except Exception:
            pass

    def _on_key(self, key):
        try:
            from pynput import keyboard as kb
            if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
                self._ctrl_down = True
            elif self._ctrl_down and getattr(key, 'vk', None) == 65:
                self.grabber.trigger()
        except Exception:
            pass

    def _on_key_up(self, key):
        try:
            from pynput import keyboard as kb
            if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
                self._ctrl_down = False
        except Exception:
            pass

    def on_text(self, text):
        self.win.after(0, self._show, text)

    def _show(self, text):
        self.cur_text = text
        self._show_panel()
        self.lbl.config(text='已选 %d 字 · %s' % (len(text), '全文' if len(text) > 500 else '选区'))
        self.btn_read.config(state='normal')
        self.btn_stop.config(state='normal')
        self.btn_pause.config(state='normal')
        x, y = self.win.winfo_pointerx() + 10, self.win.winfo_pointery() + 10
        self.win.geometry('+%d+%d' % (x, y))

    def ensure_server(self):
        try:
            requests.get(SERVER + '/health', timeout=1)
            return True
        except Exception:
            pass
        try:
            import uvicorn
            from server_core import app as srv_app
            cfg = uvicorn.Config(srv_app, host='127.0.0.1', port=8848, log_level='warning')
            self._srv = uvicorn.Server(cfg)
            threading.Thread(target=self._srv.run, daemon=True).start()
            for _ in range(40):
                time.sleep(1)
                try:
                    requests.get(SERVER + '/health', timeout=1)
                    return True
                except Exception:
                    pass
        except Exception:
            pass
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

    def _read_worker(self, text):
        text = re.sub(r'[*#|~^_`{}\[\]\\]', '', text)
        chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
        voice = VOICE_IDS[self.voice_var.get()]
        speed = float(self.speed_var.get())
        pitch = self.pitch_var.get()
        wavs = []
        try:
            for c in chunks:
                r = requests.post(SERVER + '/tts',
                                  json={'text': c, 'speed': speed, 'voice': voice, 'pitch': pitch},
                                  timeout=600)
                r.raise_for_status()
                ext = 'wav' if r.headers.get('content-type', '').endswith('wav') else 'mp3'
                tmp = os.path.join(tempfile.gettempdir(), 'dtts_%d.%s' % (time.time_ns(), ext))
                with open(tmp, 'wb') as f:
                    f.write(r.content)
                if ext == 'mp3':
                    import subprocess
                    wav = os.path.join(tempfile.gettempdir(), 'dtts_%d.wav' % time.time_ns())
                    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', tmp, '-ar', '44100',
                                    '-ac', '1', '-f', 'wav', wav], check=True,
                                   creationflags=0x08000000)
                    os.remove(tmp)
                    wavs.append(wav)
                else:
                    wavs.append(tmp)
        except Exception as e:
            self.win.after(0, lambda: self.lbl.config(text='失败: %s' % e))
            return
        self.win.after(0, lambda: self.lbl.config(text='朗读中… (可暂停/停止)'))
        self.player.play(wavs)

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
        self._show_ball()
        self.win.mainloop()


if __name__ == '__main__':
    App().run()