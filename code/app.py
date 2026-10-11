import copy
import base64
import binascii
import json
import math
import ntpath
import os
import queue
import threading
import time
import tkinter as tk
import uuid
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageDraw, ImageOps, ImageSequence, ImageTk

BG, PANEL, FG, MUTED, ACCENT = '#10151b', '#1b242e', '#edf3f8', '#9aaabd', '#ff9e45'
PRESETS = ('Lambda', 'Starfield', 'Mountains')
SCREEN_SIZES = ('128x64', '128x128', '64x128')
MAX_FRAMES = 120
ACTIONS = ('Shortcut', 'Text', 'Open app', 'Macro', 'Disabled')
STEP_ACTIONS = ('Shortcut', 'Text', 'Wait', 'Open app')
SPECIAL_KEYS = {'ENTER': 'enter', 'TAB': 'tab', 'SPACE': 'space', 'ESC': 'esc',
                'BACKSPACE': 'backspace', 'DELETE': 'delete', 'INSERT': 'insert',
                'HOME': 'home', 'END': 'end', 'PAGEUP': 'page_up', 'PAGEDOWN': 'page_down',
                'UP': 'up', 'DOWN': 'down', 'LEFT': 'left', 'RIGHT': 'right'}
MODIFIERS = {'CTRL': 'ctrl', 'ALT': 'alt', 'SHIFT': 'shift', 'WIN': 'cmd'}
ROTARY = {'clockwise': 'VOLUME_UP', 'counterclockwise': 'VOLUME_DOWN', 'press': 'MUTE'}


def shortcut_tokens(value):
    tokens = value.upper().split('+')
    tokens = [token.strip() for token in tokens]
    final = tokens[-1]
    if (any(token not in MODIFIERS for token in tokens[:-1])
            or len(set(tokens[:-1])) != len(tokens[:-1])
            or not (final in SPECIAL_KEYS or final in {f'F{i}' for i in range(1, 25)}
                    or (len(final) == 1 and final in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'))):
        raise ValueError('Use a key such as CTRL+C, ALT+TAB, ENTER, or F5. Modifiers: CTRL, ALT, SHIFT, WIN.')
    return tokens


def validate_step(step):
    if not isinstance(step, dict) or step.get('action') not in STEP_ACTIONS:
        raise ValueError('Unknown macro step.')
    action, value = step['action'], step.get('value')
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError('Each step value must be text, at most 500 characters.')
    if action == 'Shortcut':
        shortcut_tokens(value)
    elif action == 'Wait':
        try:
            seconds = float(value)
        except ValueError:
            raise ValueError('Wait must be a number of seconds.') from None
        if not math.isfinite(seconds) or not 0 <= seconds <= 60:
            raise ValueError('Wait must be between 0 and 60 seconds.')
    elif action == 'Open app':
        if (not ntpath.isabs(value) or ntpath.splitext(value)[1].lower() not in ('.exe', '.lnk')
                or any(char in value for char in '\r\n\x00"')):
            raise ValueError('Choose the full path to an .exe or .lnk file, without quotes or arguments.')


def compile_steps(key):
    """Shared sequence for desktop testing and matching USB HID firmware."""
    if key['action'] == 'Disabled':
        return []
    steps = key.get('steps', []) if key['action'] == 'Macro' else [key]
    result = []
    for step in steps:
        validate_step(step)
        action, value = step['action'], step['value']
        if action == 'Open app':
            result.extend([{'action': 'Shortcut', 'value': 'WIN+R'},
                           {'action': 'Wait', 'value': '0.6'},
                           {'action': 'Text', 'value': '"' + value + '"'},
                           {'action': 'Shortcut', 'value': 'ENTER'}])
        else:
            result.append({'action': action, 'value': value})
    return result


def run_steps(steps, keyboard, key_enum, stop):
    """Execute sequentially; always release keys and make waits cancellable."""
    for step in steps:
        if stop.is_set():
            return
        action, value = step['action'], step['value']
        if action == 'Wait':
            if stop.wait(float(value)):
                return
        elif action == 'Text':
            for char in value:
                if stop.is_set():
                    return
                keyboard.type(char)
        elif action == 'Shortcut':
            pressed = []
            try:
                for token in shortcut_tokens(value):
                    name = MODIFIERS.get(token) or SPECIAL_KEYS.get(token)
                    if token.startswith('F') and token[1:].isdigit():
                        name = token.lower()
                    code = getattr(key_enum, name) if name else token.lower()
                    keyboard.press(code)
                    pressed.append(code)
            finally:
                for code in reversed(pressed):
                    keyboard.release(code)
        if stop.wait(0.05):
            return


def screen_size(screen):
    return tuple(map(int, screen.get('size', '128x64').split('x')))


def fit_oled(image, size):
    rgba = ImageOps.exif_transpose(image).convert('RGBA')
    black = Image.new('RGBA', rgba.size, 'black')
    black.alpha_composite(rgba)
    return ImageOps.pad(black.convert('RGB'), size, method=Image.Resampling.LANCZOS,
                        color='black').convert('1')


def encode_frame(image, duration):
    return {'pixels': base64.b64encode(image.tobytes()).decode('ascii'),
            'duration_ms': max(20, min(10000, int(duration or 100)))}


def decode_frame(asset, index):
    return Image.frombytes('1', (asset['width'], asset['height']),
                           base64.b64decode(asset['frames'][index]['pixels'], validate=True))


def import_background(path, size):
    if os.path.getsize(path) > 10 * 1024 * 1024:
        raise ValueError('Choose an image or GIF smaller than 10 MB.')
    with Image.open(path) as source:
        if source.format not in ('PNG', 'JPEG', 'GIF', 'WEBP', 'BMP'):
            raise ValueError('Choose a PNG, JPG, GIF, WebP, or BMP file.')
        count = getattr(source, 'n_frames', 1)
        if count > MAX_FRAMES or source.width * source.height * count > 80000000:
            raise ValueError('Choose a shorter or smaller animation (up to 120 frames and 80 million decoded pixels).')
        frames = [encode_frame(fit_oled(frame, size), frame.info.get('duration', 100))
                  for frame in ImageSequence.Iterator(source)]
    return {'name': os.path.basename(path), 'width': size[0], 'height': size[1],
            'encoding': 'mono1-row-msb-base64', 'frames': frames}


def frame_index(asset, elapsed_ms):
    duration = sum(frame['duration_ms'] for frame in asset['frames'])
    position = elapsed_ms % duration
    for index, frame in enumerate(asset['frames']):
        if position < frame['duration_ms']:
            return index
        position -= frame['duration_ms']
    return 0


def preset_frame(name, size, phase):
    """Render the same monochrome pixels used by the preview and board payload."""
    w, h = size
    image = Image.new('1', size)
    draw = ImageDraw.Draw(image)
    if name == 'Lambda':
        radius = int(min(w, h) * (0.34 + 0.035 * math.sin(phase * math.tau)))
        x, y = w // 2, h // 2
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=255, width=2)
        draw.line([(x - radius // 3, y - radius // 2), (x, y - radius // 2),
                   (x + radius // 3, y + radius // 2), (x + radius // 2, y + radius // 2)], fill=255, width=3)
        draw.line([(x, y - radius // 4), (x - radius // 2, y + radius // 2)], fill=255, width=3)
    elif name == 'Starfield':
        for i in range(65):
            x = int((i * 83 + 19 - phase * w * (1 + i % 3)) % w)
            y = (i * 47 + 13) % h
            draw.point((x, y), fill=255)
        x, y, r = w // 2, h // 2, min(w, h) // 6
        draw.ellipse((x-r, y-r, x+r, y+r), fill=0, outline=255)
        draw.line((x-r*2, y+r//2, x+r*2, y-r//2), fill=255, width=2)
    else:
        draw.ellipse((w-24, 6, w-12, 18), outline=255)
        for layer in range(3):
            points = [(x, int(h * (0.5 + layer * 0.2) - abs(math.sin(x / w * math.tau + layer * 2 + phase * math.tau)) * h * 0.25))
                      for x in range(w)]
            draw.line(points, fill=255)
    return image


def validate_screen_media(screen):
    if screen.get('size', '128x64') not in SCREEN_SIZES:
        raise ValueError('Unsupported OLED size.')
    if (type(screen.get('animate', False)) is not bool
            or type(screen.get('speed', 1.0)) not in (int, float)
            or screen.get('speed', 1.0) not in (0.5, 1.0, 2.0)):
        raise ValueError('Invalid animation playback setting.')
    asset = screen.get('asset')
    if screen['background'] == 'Custom' and asset is None:
        raise ValueError('Upload an image before selecting Custom.')
    if asset is None:
        return
    if (not isinstance(asset, dict) or asset.get('encoding') != 'mono1-row-msb-base64'
            or not isinstance(asset.get('name'), str) or len(asset['name']) > 255
            or type(asset.get('width')) is not int or type(asset.get('height')) is not int
            or (asset.get('width'), asset.get('height')) != screen_size(screen)):
        raise ValueError('Invalid embedded background.')
    frames = asset.get('frames')
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        raise ValueError('Background must contain 1–120 frames.')
    expected = ((asset['width'] + 7) // 8) * asset['height']
    for frame in frames:
        if (not isinstance(frame, dict) or type(frame.get('duration_ms')) is not int
                or not 20 <= frame['duration_ms'] <= 10000 or not isinstance(frame.get('pixels'), str)
                or len(frame['pixels']) != 4 * ((expected + 2) // 3)):
            raise ValueError('Invalid background frame or timing.')
        try:
            pixels = base64.b64decode(frame['pixels'], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('Invalid background pixel data.') from None
        if len(pixels) != expected:
            raise ValueError('Background frame has the wrong dimensions.')


def device_config(profile):
    data = validate_profile(profile)
    advanced = any(key['action'] in ('Open app', 'Macro') for key in data['keys'])
    screen = data['screen']
    media = screen['mode'] == 'Background' and (screen['background'] == 'Custom' or screen.get('animate', False))
    data['version'] = 2 if advanced or media else 1
    for key in data['keys']:
        if advanced:
            key['compiled_steps'] = compile_steps(key)
        else:
            key.pop('steps', None)
    if media:
        if screen['background'] == 'Custom':
            asset = copy.deepcopy(screen['asset'])
        else:
            size = screen_size(screen)
            asset = {'name': screen['background'], 'width': size[0], 'height': size[1],
                     'encoding': 'mono1-row-msb-base64', 'frames': [
                         encode_frame(preset_frame(screen['background'], size, i / 16), 100) for i in range(16)]}
        if not screen.get('animate', False):
            asset['frames'] = asset['frames'][:1]
        for frame in asset['frames']:
            frame['duration_ms'] = max(20, round(frame['duration_ms'] / screen.get('speed', 1.0)))
        screen['playback'] = asset
    screen.pop('asset', None)
    if not media and screen['background'] == 'Custom':
        screen['background'] = 'Lambda'
    for field in ('animate', 'speed', 'size'):
        screen.pop(field, None)
    if len(json.dumps(data, ensure_ascii=True).encode('utf-8')) > 64000:
        raise ValueError('Profile is too large for the board. Use a shorter GIF, a smaller OLED size, or shorter macros.')
    return data


def default_profile():
    shortcuts = ['CTRL+C', 'CTRL+V', 'CTRL+Z', 'CTRL+Y', 'CTRL+A', 'CTRL+S',
                 'CTRL+F', 'CTRL+SHIFT+S', 'ALT+TAB', 'CTRL+W', 'CTRL+T', 'WIN+D']
    return {'version': 2, 'keys': [
        {'id': i + 1, 'label': f'Key {i + 1}', 'action': 'Shortcut', 'value': value}
        for i, value in enumerate(shortcuts)], 'rotary': dict(ROTARY),
        'screen': {'mode': 'Background', 'background': 'Lambda', 'todos': [],
                   'size': '128x64', 'animate': True, 'speed': 1.0}}


def validate_profile(data):
    if not isinstance(data, dict) or data.get('version') not in (1, 2):
        raise ValueError('Unsupported profile version.')
    data = copy.deepcopy(data)
    data['version'] = 2
    keys = data.get('keys')
    if not isinstance(keys, list) or len(keys) != 12:
        raise ValueError('A profile must contain exactly 12 keys.')
    for number, key in enumerate(keys, 1):
        if not isinstance(key, dict) or key.get('id') != number:
            raise ValueError('Key IDs must be 1 through 12, in order.')
        if key.get('action') not in ACTIONS:
            raise ValueError(f'Invalid action on key {number}.')
        for field, limit in [('label', 24), ('value', 500)]:
            if not isinstance(key.get(field), str) or len(key[field]) > limit:
                raise ValueError(f'Key {number}: {field} exceeds {limit} characters.')
        if key['action'] == 'Macro':
            steps = key.get('steps', [])
            if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
                raise ValueError(f'Key {number}: add between 1 and 32 macro steps.')
        compile_steps(key)
    screen = data.get('screen')
    if not isinstance(screen, dict) or screen.get('mode') not in ('Background', 'To-do list'):
        raise ValueError('Invalid screen mode.')
    if screen.get('background') not in (*PRESETS, 'Custom'):
        raise ValueError('Unknown background.')
    validate_screen_media(screen)
    todos = screen.get('todos')
    if not isinstance(todos, list) or len(todos) > 20:
        raise ValueError('Use at most 20 tasks.')
    for task in todos:
        if (not isinstance(task, dict) or not isinstance(task.get('text'), str)
                or not task['text'].strip() or len(task['text']) > 80
                or type(task.get('done')) is not bool):
            raise ValueError('Each task needs text (1–80 characters) and a done flag.')
    # Volume controls are deliberately fixed, including when loading a profile.
    data = copy.deepcopy(data)
    data['rotary'] = dict(ROTARY)
    return data


class MacroEditor:
    def __init__(self, app):
        self.app = app
        self.key_index = app.selected
        self.steps = copy.deepcopy(app.profile['keys'][self.key_index].get('steps', []))
        self.edit_index = None
        self.window = tk.Toplevel(app.root)
        self.window.title(f'Macro builder · Key {self.key_index + 1}')
        self.window.geometry('700x550')
        self.window.configure(bg=BG)
        self.window.transient(app.root)
        self.window.grab_set()
        frame = ttk.Frame(self.window, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Build a sequence', font=('Segoe UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Select a step to edit it. Save includes your latest edits automatically.', style='Muted.TLabel').pack(anchor='w', pady=8)
        self.listbox = tk.Listbox(frame, bg=PANEL, fg=FG, selectbackground='#725034', exportselection=False)
        self.listbox.pack(fill='both', expand=True)
        self.listbox.bind('<<ListboxSelect>>', self.select)
        self.action, self.value = tk.StringVar(value='Shortcut'), tk.StringVar(value='CTRL+C')
        ttk.Combobox(frame, textvariable=self.action, values=STEP_ACTIONS, state='readonly').pack(fill='x', pady=8)
        ttk.Entry(frame, textvariable=self.value).pack(fill='x')
        ttk.Label(frame, text='Shortcut: CTRL+C · Wait: seconds · Text: what to type · Open app: browse below', style='Muted.TLabel').pack(anchor='w', pady=8)
        row = ttk.Frame(frame)
        row.pack(fill='x')
        for label, callback in [('Browse app', self.browse), ('Add', self.add), ('Update', self.update),
                                ('Remove', self.remove), ('↑', lambda: self.move(-1)), ('↓', lambda: self.move(1))]:
            ttk.Button(row, text=label, command=callback).pack(side='left', padx=2)
        ttk.Label(frame, text='Example: Open app → Wait 2 → Text Hello → Shortcut ENTER', style='Muted.TLabel').pack(anchor='w', pady=12)
        ttk.Button(frame, text='Save macro to this key', command=self.save).pack(anchor='e')
        self.refresh(0 if self.steps else None)

    def refresh(self, index=None):
        self.listbox.delete(0, 'end')
        for i, step in enumerate(self.steps):
            self.listbox.insert('end', f"{i + 1:02}   {step['action']}   {step['value']}")
        if index is not None:
            self.listbox.selection_set(index)
            self.listbox.see(index)
            self.edit_index = index
            step = self.steps[index]
            self.action.set(step['action'])
            self.value.set(step['value'])
        else:
            self.edit_index = None

    def commit_pending(self):
        if self.edit_index is None:
            return True
        step = self.read_step()
        if step is None:
            return False
        self.steps[self.edit_index] = step
        return True

    def select(self, _=None):
        selected = self.listbox.curselection()
        if selected and selected[0] != self.edit_index:
            target = selected[0]
            if not self.commit_pending():
                self.listbox.selection_clear(0, 'end')
                if self.edit_index is not None:
                    self.listbox.selection_set(self.edit_index)
                return
            self.refresh(target)

    def browse(self):
        path = filedialog.askopenfilename(parent=self.window, title='Choose an app or shortcut',
                                          filetypes=[('Windows apps', '*.exe *.lnk')])
        if path:
            self.action.set('Open app')
            self.value.set(ntpath.normpath(path))

    def read_step(self):
        step = {'action': self.action.get(), 'value': self.value.get()}
        try:
            validate_step(step)
            return step
        except ValueError as error:
            messagebox.showerror('Invalid step', str(error), parent=self.window)

    def add(self):
        if len(self.steps) >= 32:
            messagebox.showerror('Macro limit', 'Use at most 32 steps.', parent=self.window)
            return
        step = self.read_step()
        if step:
            self.steps.append(step)
            self.refresh(len(self.steps) - 1)

    def update(self):
        if self.commit_pending() and self.edit_index is not None:
            self.refresh(self.edit_index)

    def remove(self):
        selected = self.listbox.curselection()
        if selected:
            del self.steps[selected[0]]
            self.refresh()

    def move(self, offset):
        selected = self.listbox.curselection()
        if selected and 0 <= selected[0] + offset < len(self.steps):
            if not self.commit_pending():
                return
            old, new = selected[0], selected[0] + offset
            self.steps[old], self.steps[new] = self.steps[new], self.steps[old]
            self.refresh(new)

    def save(self):
        if not self.commit_pending():
            return
        if not self.steps:
            messagebox.showerror('Empty macro', 'Add at least one step.', parent=self.window)
            return
        try:
            for step in self.steps:
                validate_step(step)
        except ValueError as error:
            messagebox.showerror('Invalid macro', str(error), parent=self.window)
            return
        key = self.app.profile['keys'][self.key_index]
        key.update(action='Macro', value='', steps=copy.deepcopy(self.steps))
        self.app.select(self.key_index)
        self.app.changed()
        self.window.destroy()


class Studio:
    def __init__(self, root):
        self.root, self.profile, self.selected = root, default_profile(), 0
        self.dirty, self.loading, self.sending = False, False, False
        self.events = queue.Queue()
        self.test_stop = threading.Event()
        self.testing = False
        self.preview_elapsed = 0.0
        self.preview_last = time.monotonic()
        self.preview_photo = None
        self.importing = False
        root.title('Macroboard Studio')
        root.geometry('1120x860')
        root.minsize(1040, 840)
        root.configure(bg=BG)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', background=BG, foreground=FG, font=('Segoe UI', 10))
        style.configure('TButton', padding=8, background=PANEL)
        style.map('TButton', background=[('active', '#354355')])
        style.configure('TEntry', fieldbackground=PANEL, foreground=FG)
        style.configure('TCombobox', fieldbackground=PANEL, foreground=FG)
        style.map('TCombobox', fieldbackground=[('readonly', PANEL)], foreground=[('readonly', FG)])
        style.configure('Title.TLabel', font=('Segoe UI', 24, 'bold'))
        style.configure('Muted.TLabel', foreground=MUTED)
        page = ttk.Frame(root, padding=24)
        page.pack(fill='both', expand=True)
        ttk.Label(page, text='MACROBOARD / STUDIO', style='Title.TLabel').pack(anchor='w')
        ttk.Label(page, text='12 keys. Your shortcuts. A little personality.', style='Muted.TLabel').pack(anchor='w', pady=(0, 16))
        toolbar = ttk.Frame(page)
        toolbar.pack(fill='x', pady=(0, 16))
        for label, command in [('Open profile', self.load), ('Save profile', self.save)]:
            ttk.Button(toolbar, text=label, command=command).pack(side='left', padx=(0, 8))
        self.port = tk.StringVar()
        self.ports = ttk.Combobox(toolbar, textvariable=self.port, width=13, state='readonly')
        self.ports.pack(side='left', padx=8)
        ttk.Button(toolbar, text='Refresh USB', command=self.refresh_ports).pack(side='left')
        self.send_button = ttk.Button(toolbar, text='Apply to board', command=self.send)
        self.send_button.pack(side='right')
        columns = ttk.Frame(page)
        columns.pack(fill='both', expand=True)
        left, right = ttk.Frame(columns), ttk.Frame(columns)
        left.pack(side='left', fill='both', expand=True, padx=(0, 24))
        right.pack(side='left', fill='both', expand=True)
        ttk.Label(left, text='01 / INDIVIDUAL KEYS', foreground=ACCENT).pack(anchor='w')
        grid = ttk.Frame(left)
        grid.pack(fill='x', pady=12)
        self.buttons = []
        for i in range(12):
            button = tk.Button(grid, height=3, width=12, relief='flat', bg=PANEL,
                               fg=FG, activebackground=ACCENT, wraplength=100,
                               command=lambda n=i: self.select(n))
            button.grid(row=i // 4, column=i % 4, padx=3, pady=3, sticky='nsew')
            self.buttons.append(button)
        for col in range(4):
            grid.columnconfigure(col, weight=1)
        self.key_title = tk.StringVar()
        ttk.Label(left, textvariable=self.key_title, font=('Segoe UI', 14, 'bold')).pack(anchor='w', pady=(10, 8))
        self.label, self.action, self.value = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for caption, variable in [('Button label (24 characters)', self.label), ('Action', self.action), ('Shortcut, text, or app path', self.value)]:
            ttk.Label(left, text=caption).pack(anchor='w', pady=(5, 3))
            widget = (ttk.Combobox(left, textvariable=variable, values=ACTIONS, state='readonly')
                      if variable is self.action else ttk.Entry(left, textvariable=variable))
            widget.pack(fill='x')
            if variable is self.value:
                self.value_entry = widget
        key_tools = ttk.Frame(left)
        key_tools.pack(fill='x', pady=(8, 0))
        ttk.Button(key_tools, text='Browse app', command=self.browse_app).pack(side='left', padx=(0, 5))
        ttk.Button(key_tools, text='Build macro', command=lambda: MacroEditor(self)).pack(side='left')
        self.test_button = ttk.Button(key_tools, text='Test key (3s)', command=self.test_key)
        self.test_button.pack(side='left', padx=5)
        ttk.Button(key_tools, text='Stop', command=self.test_stop.set).pack(side='left')
        self.key_hint = tk.StringVar()
        ttk.Label(left, textvariable=self.key_hint, style='Muted.TLabel', wraplength=490).pack(anchor='w', pady=10)
        ttk.Separator(left).pack(fill='x', pady=12)
        ttk.Label(left, text='02 / ROTARY · VOLUME ONLY', foreground=ACCENT).pack(anchor='w')
        ttk.Label(left, text='Clockwise       Volume up\nCounterclockwise       Volume down\nPress       Mute / unmute', justify='left').pack(anchor='w', pady=12)
        ttk.Label(right, text='03 / OLED SCREEN', foreground=ACCENT).pack(anchor='w')
        self.canvas = tk.Canvas(right, height=205, bg='#000000', highlightthickness=1, highlightbackground='#34404d')
        self.canvas.pack(fill='x', pady=12)
        self.canvas.bind('<Configure>', lambda _: self.draw())
        ttk.Label(right, text='Illustrative preview · monochrome OLED', style='Muted.TLabel').pack(anchor='w')
        self.mode, self.background = tk.StringVar(), tk.StringVar()
        self.animate = tk.BooleanVar(value=False)
        self.speed = tk.StringVar(value='1x')
        self.size = tk.StringVar(value='128x64')
        ttk.Label(right, text='Screen mode').pack(anchor='w', pady=(10, 3))
        ttk.Combobox(right, textvariable=self.mode, values=('Background', 'To-do list'), state='readonly').pack(fill='x')
        self.background_panel = ttk.Frame(right)
        self.todo_panel = ttk.Frame(right)
        panel = self.background_panel
        ttk.Label(panel, text='Background design').pack(anchor='w', pady=(10, 3))
        self.background_menu = ttk.Combobox(panel, textvariable=self.background, values=PRESETS, state='readonly')
        self.background_menu.pack(fill='x')
        upload_row = ttk.Frame(panel)
        upload_row.pack(fill='x', pady=10)
        self.upload_button = ttk.Button(upload_row, text='Upload image / GIF', command=self.upload_background)
        self.upload_button.pack(side='left', padx=(0, 8))
        ttk.Checkbutton(upload_row, text='Animate', variable=self.animate).pack(side='left')
        settings = ttk.Frame(panel)
        settings.pack(fill='x')
        ttk.Label(settings, text='OLED size').pack(side='left')
        ttk.Combobox(settings, textvariable=self.size, values=SCREEN_SIZES, width=10, state='readonly').pack(side='left', padx=6)
        ttk.Label(settings, text='Speed').pack(side='left')
        ttk.Combobox(settings, textvariable=self.speed, values=('0.5x', '1x', '2x'), width=5, state='readonly').pack(side='left', padx=6)
        self.media_info = tk.StringVar(value='Upload PNG, JPG, GIF, WebP, or BMP. Images are fitted to the OLED in black and white.')
        ttk.Label(panel, textvariable=self.media_info, style='Muted.TLabel', wraplength=430).pack(anchor='w', pady=12)
        ttk.Label(panel, text='Animate plays GIFs and moves the built-in designs.\nImages travel with your saved profile.', style='Muted.TLabel').pack(anchor='w')
        ttk.Label(self.todo_panel, text='Tasks · double-click to complete').pack(anchor='w', pady=(12, 4))
        self.tasks = tk.Listbox(self.todo_panel, height=5, bg=PANEL, fg=FG, selectbackground='#725034', relief='flat', exportselection=False)
        self.tasks.pack(fill='both', expand=True)
        self.tasks.bind('<Double-Button-1>', lambda _: self.toggle_task())
        self.task_text = tk.StringVar()
        entry = ttk.Entry(self.todo_panel, textvariable=self.task_text)
        entry.pack(fill='x', pady=6)
        entry.bind('<Return>', lambda _: self.add_task())
        actions = ttk.Frame(self.todo_panel)
        actions.pack(fill='x')
        for label, command in [('Add task', self.add_task), ('Done / undo', self.toggle_task), ('Remove', self.remove_task)]:
            ttk.Button(actions, text=label, command=command).pack(side='left', padx=(0, 5))
        self.status = tk.StringVar(value='Local editor ready. USB requires compatible macroboard firmware.')
        ttk.Label(page, textvariable=self.status, style='Muted.TLabel', wraplength=990).pack(anchor='w', pady=(18, 0))
        for variable in (self.label, self.action, self.value):
            variable.trace_add('write', self.edit_key)
        for variable in (self.mode, self.animate, self.speed):
            variable.trace_add('write', self.edit_screen)
        self.background.trace_add('write', self.choose_background)
        self.size.trace_add('write', self.resize_background)
        self.populate()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)
        self.preview_job = root.after(40, self.animate_preview)

    def changed(self):
        self.dirty = True
        self.root.title('Macroboard Studio • Unsaved changes')

    def populate(self):
        self.loading = True
        self.mode.set(self.profile['screen']['mode'])
        self.background.set(self.profile['screen']['background'])
        self.animate.set(self.profile['screen'].get('animate', False))
        self.speed.set(f"{self.profile['screen'].get('speed', 1.0):g}x")
        self.size.set(self.profile['screen'].get('size', '128x64'))
        self.loading = False
        self.preview_elapsed = 0.0
        self.refresh_screen_controls()
        self.select(self.selected)
        self.refresh_tasks()

    def select(self, index):
        self.selected, self.loading = index, True
        key = self.profile['keys'][index]
        self.key_title.set(f'Editing key {index + 1:02}')
        self.label.set(key['label'])
        self.action.set(key['action'])
        self.value.set(key['value'])
        self.loading = False
        self.update_key_controls()
        self.refresh_keys()

    def update_key_controls(self):
        action = self.action.get()
        self.value_entry.configure(state='disabled' if action in ('Disabled', 'Macro') else 'normal')
        count = len(self.profile['keys'][self.selected].get('steps', []))
        hints = {'Shortcut': 'Examples: CTRL+C, CTRL+SHIFT+S, WIN+D',
                 'Text': 'Types into the focused app. Test gives you 3 seconds to switch windows.',
                 'Open app': 'Choose an .exe or .lnk. Launches through Windows Run.',
                 'Macro': f'{count} steps saved. Use Build macro to edit or reorder them.',
                 'Disabled': 'This key performs no action.'}
        self.key_hint.set(hints[action])

    def browse_app(self):
        path = filedialog.askopenfilename(title='Choose an app or shortcut', filetypes=[('Windows apps', '*.exe *.lnk')])
        if path:
            self.action.set('Open app')
            self.value.set(ntpath.normpath(path))

    def test_key(self):
        if self.testing:
            return
        key = copy.deepcopy(self.profile['keys'][self.selected])
        try:
            if key['action'] == 'Macro' and not key.get('steps'):
                raise ValueError('Add macro steps first.')
            steps = compile_steps(key)
            originals = key.get('steps', []) if key['action'] == 'Macro' else [key]
            for step in originals:
                if step['action'] == 'Open app' and not os.path.isfile(step['value']):
                    raise ValueError('App file not found: ' + step['value'])
            if not steps:
                self.status.set('This key is disabled.')
                return
            from pynput.keyboard import Controller, Key
        except (ValueError, ImportError) as error:
            messagebox.showerror('Cannot test key', str(error) + '\nFor keyboard testing install: python -m pip install pynput')
            return
        self.testing = True
        self.test_stop.clear()
        self.test_button.configure(state='disabled')
        self.status.set('Testing in 3 seconds — focus the target app. Stop cancels remaining steps.')

        def worker():
            try:
                if not self.test_stop.wait(3):
                    run_steps(steps, Controller(), Key, self.test_stop)
                self.events.put(('test_ok', 'Test stopped.' if self.test_stop.is_set() else 'Test sequence sent to Windows.'))
            except Exception as error:
                self.events.put(('test_error', str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def refresh_keys(self):
        for i, button in enumerate(self.buttons):
            key = self.profile['keys'][i]
            button.configure(text=f"{i + 1:02}\n{key['label']}", bg=ACCENT if i == self.selected else PANEL,
                             fg=BG if i == self.selected else FG)

    def edit_key(self, *_):
        if self.loading:
            return
        self.profile['keys'][self.selected].update(label=self.label.get(), action=self.action.get(), value=self.value.get())
        self.update_key_controls()
        self.changed()
        self.refresh_keys()

    def choose_background(self, *_):
        if self.loading:
            return
        # A still-image upload must not leave subsequently selected presets paused.
        self.loading = True
        asset = self.profile['screen'].get('asset')
        self.animate.set(self.background.get() in PRESETS or bool(asset and len(asset['frames']) > 1))
        self.loading = False
        self.edit_screen()

    def edit_screen(self, *_):
        if not self.loading:
            self.profile['screen'].update(mode=self.mode.get(), background=self.background.get(),
                                          animate=self.animate.get(), speed=float(self.speed.get()[:-1]))
            self.preview_elapsed = 0.0
            self.preview_last = time.monotonic()
            self.refresh_screen_controls()
            self.changed()
            self.draw()

    def refresh_screen_controls(self):
        self.background_panel.pack_forget()
        self.todo_panel.pack_forget()
        panel = self.background_panel if self.mode.get() == 'Background' else self.todo_panel
        panel.pack(fill='both', expand=True)
        asset = self.profile['screen'].get('asset')
        self.background_menu.configure(values=(*PRESETS, 'Custom') if asset else PRESETS)
        if self.background.get() in PRESETS:
            descriptions = {'Lambda': 'Pulsing lambda emblem', 'Starfield': 'Drifting stars around a ringed planet',
                            'Mountains': 'Moving mountain layers'}
            state = 'Playing' if self.animate.get() else 'Paused — enable Animate to play'
            self.media_info.set(f"{self.background.get()} · {descriptions[self.background.get()]}\n{state}. No upload needed.")
        elif asset:
            self.media_info.set(f"{asset['name']} · {asset['width']}×{asset['height']} · {len(asset['frames'])} frame(s)\nEmbedded in this profile; the original file is no longer needed.")
        else:
            self.media_info.set('Upload PNG, JPG, GIF, WebP, or BMP. Images are fitted to the OLED in black and white.')

    def resize_background(self, *_):
        if self.loading:
            return
        screen = self.profile['screen']
        screen['size'] = self.size.get()
        asset = screen.get('asset')
        if asset:
            size = screen_size(screen)
            frames = [encode_frame(fit_oled(decode_frame(asset, i), size), frame['duration_ms'])
                      for i, frame in enumerate(asset['frames'])]
            screen['asset'] = dict(asset, width=size[0], height=size[1], frames=frames)
        self.edit_screen()

    def upload_background(self):
        if self.importing:
            return
        path = filedialog.askopenfilename(title='Upload OLED background',
                                         filetypes=[('Images and GIFs', '*.png *.jpg *.jpeg *.gif *.webp *.bmp')])
        if not path:
            return
        self.importing = True
        self.upload_button.configure(state='disabled')
        self.status.set('Converting background to OLED pixels…')
        size = screen_size(self.profile['screen'])

        def worker():
            try:
                self.events.put(('media_ok', import_background(path, size)))
            except Exception as error:
                self.events.put(('media_error', str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def apply_background(self, asset):
        # A user can resize the target while the import worker is running.
        size = screen_size(self.profile['screen'])
        if (asset['width'], asset['height']) != size:
            frames = [encode_frame(fit_oled(decode_frame(asset, i), size), frame['duration_ms'])
                      for i, frame in enumerate(asset['frames'])]
            asset = dict(asset, width=size[0], height=size[1], frames=frames)
        self.profile['screen']['asset'] = asset
        self.loading = True
        self.mode.set('Background')
        self.background.set('Custom')
        self.animate.set(len(asset['frames']) > 1)
        self.loading = False
        self.edit_screen()

    def animate_preview(self):
        now = time.monotonic()
        if self.animate.get() and self.mode.get() == 'Background':
            self.preview_elapsed += (now - self.preview_last) * 1000 * float(self.speed.get()[:-1])
            self.draw()
        self.preview_last = now
        self.preview_job = self.root.after(40, self.animate_preview)

    def add_task(self):
        text = self.task_text.get().strip()
        tasks = self.profile['screen']['todos']
        if not text:
            return
        if len(text) > 80 or len(tasks) >= 20:
            messagebox.showerror('Task limit', 'Use up to 20 tasks, each at most 80 characters.')
            return
        tasks.append({'text': text, 'done': False})
        self.task_text.set('')
        self.changed()
        self.refresh_tasks()

    def toggle_task(self):
        selection = self.tasks.curselection()
        if selection:
            task = self.profile['screen']['todos'][selection[0]]
            task['done'] = not task['done']
            self.changed()
            self.refresh_tasks()

    def remove_task(self):
        selection = self.tasks.curselection()
        if selection:
            del self.profile['screen']['todos'][selection[0]]
            self.changed()
            self.refresh_tasks()

    def refresh_tasks(self):
        self.tasks.delete(0, 'end')
        for task in self.profile['screen']['todos']:
            self.tasks.insert('end', ('✓  ' if task['done'] else '○  ') + task['text'])
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete('all')
        w, h = max(c.winfo_width(), 320), 205
        if self.mode.get() == 'To-do list':
            c.create_text(18, 18, text='TODAY / TO DO', fill='white', anchor='nw', font=('Consolas', 13, 'bold'))
            tasks = self.profile['screen']['todos']
            if not tasks:
                c.create_text(w / 2, 105, text='A fresh start. Add a task below.', fill='white')
            for i, task in enumerate(tasks[:5]):
                text = ('[x] ' if task['done'] else '[ ] ') + task['text']
                c.create_text(18, 53 + i * 25, text=text[:39], fill='white', anchor='nw', font=('Consolas', 10))
            if len(tasks) > 5:
                c.create_text(w - 12, 195, text=f'+{len(tasks) - 5} more', fill='white', anchor='se')
        else:
            screen = self.profile['screen']
            elapsed = self.preview_elapsed if self.animate.get() else 0
            if self.background.get() == 'Custom' and screen.get('asset'):
                asset = screen['asset']
                bitmap = decode_frame(asset, frame_index(asset, elapsed))
            else:
                phase = (int(elapsed / 100) % 16) / 16
                bitmap = preset_frame(self.background.get(), screen_size(screen), phase)
            scale = min((w - 16) / bitmap.width, (h - 12) / bitmap.height)
            resized = bitmap.resize((max(1, int(bitmap.width * scale)), max(1, int(bitmap.height * scale))), Image.Resampling.NEAREST)
            # Tk can display raw mode-1 value 1 as RGB(1,1,1), not white.
            self.preview_photo = ImageTk.PhotoImage(resized.convert('RGB'), master=self.root)
            c.create_image(w / 2, h / 2, image=self.preview_photo)

    def confirm_discard(self):
        return not self.dirty or messagebox.askyesno('Unsaved profile', 'Discard unsaved profile changes?')

    def save(self):
        try:
            data = validate_profile(self.profile)
            path = filedialog.asksaveasfilename(defaultextension='.json', filetypes=[('Profile', '*.json')])
            if path:
                with open(path, 'w', encoding='utf-8') as file:
                    json.dump(data, file, indent=2, ensure_ascii=False)
                self.dirty = False
                self.root.title('Macroboard Studio')
                self.status.set('Profile saved to your computer.')
        except (ValueError, OSError) as error:
            messagebox.showerror('Save failed', str(error))

    def load(self):
        if self.importing:
            messagebox.showinfo('Background import', 'Wait for the background import to finish before opening another profile.')
            return
        if not self.confirm_discard():
            return
        path = filedialog.askopenfilename(filetypes=[('Profile', '*.json')])
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as file:
                data = validate_profile(json.load(file))
            self.profile = data
            self.populate()
            self.dirty = False
            self.root.title('Macroboard Studio')
            self.status.set('Profile loaded. Apply to board when ready.')
        except (ValueError, OSError) as error:
            messagebox.showerror('Cannot open profile', str(error))

    def refresh_ports(self):
        try:
            from serial.tools import list_ports
            ports = [port.device for port in list_ports.comports()]
            self.ports.configure(values=ports)
            if self.port.get() not in ports:
                self.port.set(ports[0] if ports else '')
            self.status.set('Choose the board data port.' if ports else 'No serial ports found. Connect the board and refresh.')
        except ImportError:
            messagebox.showinfo('USB package needed', 'Run: python -m pip install pyserial\nLocal editing works without this package.')

    def send(self):
        if self.sending:
            return
        if not self.port.get():
            messagebox.showinfo('Select a port', 'Click Refresh USB and select the board data port first.')
            return
        try:
            data = device_config(self.profile)
        except ValueError as error:
            messagebox.showerror('Invalid profile', str(error))
            return
        self.sending = True
        self.send_button.configure(state='disabled')
        self.status.set('Waiting for compatible firmware…')
        threading.Thread(target=self.transfer, args=(self.port.get(), data), daemon=True).start()

    def transfer(self, port, profile):
        try:
            import serial
            with serial.Serial(port, 115200, timeout=0.1, write_timeout=2) as device:
                time.sleep(1.5)
                device.reset_input_buffer()

                def request(command, **payload):
                    request_id = uuid.uuid4().hex
                    packet = dict(command=command, request_id=request_id, **payload)
                    device.write((json.dumps(packet, ensure_ascii=True) + '\n').encode('utf-8'))
                    deadline, buffer = time.monotonic() + 5, bytearray()
                    while time.monotonic() < deadline:
                        buffer.extend(device.read(min(device.in_waiting or 1, 4096)))
                        if len(buffer) > 65536:
                            raise ValueError('Unexpectedly large response from board.')
                        while b'\n' in buffer:
                            line, _, rest = buffer.partition(b'\n')
                            buffer = bytearray(rest)
                            try:
                                reply = json.loads(line)
                            except (ValueError, UnicodeError):
                                continue
                            if not isinstance(reply, dict) or reply.get('request_id') != request_id:
                                continue
                            if reply.get('ok') is not True:
                                raise ValueError(str(reply.get('error', 'Board rejected the command.')))
                            return reply
                    raise TimeoutError('No matching acknowledgement. Check firmware and the selected USB data port.')

                hello = request('hello', protocol=1)
                if hello.get('device') != 'macroboard' or hello.get('protocol') != 1:
                    raise ValueError('The connected device does not support Macroboard protocol 1.')
                if any('compiled_steps' in key for key in profile['keys']) and 'macro_steps_v1' not in hello.get('capabilities', []):
                    raise ValueError('Board firmware needs macro_steps_v1 support for Open app and Macro actions.')
                if 'playback' in profile['screen'] and 'oled_frames_v1' not in hello.get('capabilities', []):
                    raise ValueError('Board firmware needs oled_frames_v1 support for uploaded and animated backgrounds.')
                request('set_config', config=profile)
            self.events.put(('ok', 'Board acknowledged the configuration. Device persistence depends on firmware.'))
        except Exception as error:
            self.events.put(('error', str(error)))

    def poll(self):
        try:
            kind, text = self.events.get_nowait()
            if kind.startswith('media_'):
                self.importing = False
                self.upload_button.configure(state='normal')
                if kind == 'media_ok':
                    self.apply_background(text)
                    text = 'Background imported. Save your profile to keep it.'
            elif kind.startswith('test_'):
                self.testing = False
                self.test_button.configure(state='normal')
            else:
                self.sending = False
                self.send_button.configure(state='normal')
            self.status.set(text)
            if kind in ('error', 'test_error', 'media_error'):
                title = {'error': 'USB transfer failed', 'test_error': 'Test failed', 'media_error': 'Background import failed'}[kind]
                messagebox.showerror(title, text)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self):
        if self.importing:
            messagebox.showinfo('Background import', 'Wait for the background import to finish before closing.')
            return
        if self.testing:
            self.test_stop.set()
            self.status.set('Stopping test. Close again once it finishes.')
            return
        if self.sending:
            messagebox.showinfo('Transfer in progress', 'Wait for the USB transfer to finish before closing.')
        elif self.confirm_discard():
            self.root.after_cancel(self.preview_job)
            self.root.destroy()


if __name__ == '__main__':
    Studio(tk.Tk()).root.mainloop()
