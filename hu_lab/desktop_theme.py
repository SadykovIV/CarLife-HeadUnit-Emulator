"""Shared dark/light desktop palettes; video pixels are never recolored."""
import re

DARK_STYLE = '''
QMainWindow, QWidget { background: #10151e; color: #e8edf5; font-family: "Segoe UI", ".AppleSystemUIFont", "Arial"; font-size: 13px; }
QWidget#header { background: #151d29; border-bottom: 1px solid #293346; }
QLabel#brand { font-size: 18px; font-weight: 700; color: #f1f7fb; }
QLabel#muted { color: #9aa7bb; font-size: 12px; }
QLabel#state { color: #67c5c0; background: #203b35; border-radius: 12px; padding: 7px 13px; font-weight: 600; }
QLabel#notice { background: #1c2a36; color: #aec5d8; padding: 9px; border-radius: 6px; }
QLabel#error { background: #452927; color: #ffbdad; padding: 10px; border-radius: 6px; }
QGroupBox { border: 1px solid #2b3946; border-radius: 8px; margin-top: 17px; padding: 14px 10px 10px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #94adc2; }
QPushButton { background: #253145; border: 1px solid #364858; border-radius: 6px; padding: 8px 12px; }
QPushButton:hover { background: #30425a; }
QPushButton:pressed { background: #426178; }
QPushButton:disabled { color: #61717f; background: #1b252e; border-color: #25323d; }
QPushButton#primary { background: #67c5c0; border: 0; color: #102820; font-weight: 700; padding: 10px; }
QPushButton#primary:hover { background: #93efd3; }
QPushButton#primary:disabled { background: #263e38; color: #79958d; }
QLabel { background: transparent; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #101823; border: 1px solid #364858; border-radius: 5px; padding: 7px; selection-background-color: #3d7d6a; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: #20303e; selection-background-color: #365f58; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: transparent; padding: 9px 16px; margin: 0 4px; color: #9aa7bb; border: 1px solid transparent; border-radius: 7px; }
QTabBar::tab:selected { color: #8cddd7; background: #1e353c; border: 1px solid #30545a; }
QPlainTextEdit { background: #0c1218; border: 1px solid #293947; border-radius: 6px; padding: 6px; font-family: "Menlo"; font-size: 11px; }
QSlider::groove:horizontal { background: #344756; height: 5px; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #67c5c0; }
QSlider::handle:horizontal { background: #d5fff1; width: 14px; margin: -5px 0; border-radius: 7px; }
QScrollArea { border: 0; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #577185; border-radius: 4px; background: #101823; }
QCheckBox::indicator:checked { background: #67c5c0; border-color: #67c5c0; }
QSplitter::handle { background: #293346; width: 1px; }
QWidget#inspector { background: #192231; border-radius: 10px; }
QLabel#badge { color: #8cddd7; border: 1px solid #67c5c0; border-radius: 10px; font-weight: 700; }
QLabel#footer { color: #9aa7bb; padding: 9px 18px; border-top: 1px solid #293346; }
'''

LIGHT_COLORS = {'#10151e': '#f4f6f9', '#e8edf5': '#202b3b', '#151d29': '#ffffff', '#293346': '#d5dde8', '#f1f7fb': '#152b45', '#9aa7bb': '#596c82', '#67c5c0': '#167d95', '#203b35': '#def1ed', '#1c2a36': '#e6eff7', '#aec5d8': '#345975', '#452927': '#fff0ed', '#ffbdad': '#a72e20', '#2b3946': '#d5dde8', '#94adc2': '#526c85', '#253145': '#ffffff', '#364858': '#c5d0de', '#30425a': '#e4edf7', '#426178': '#d2e1f1', '#61717f': '#8491a1', '#1b252e': '#edf1f5', '#25323d': '#dce3eb', '#102820': '#ffffff', '#93efd3': '#2494ab', '#263e38': '#dae8ec', '#79958d': '#738990', '#101823': '#ffffff', '#3d7d6a': '#b9e7ec', '#20303e': '#ffffff', '#365f58': '#cfedf0', '#8cddd7': '#11687d', '#1e353c': '#dff0f4', '#30545a': '#acd4dd', '#0c1218': '#ffffff', '#293947': '#cbd6e2', '#344756': '#c5d2df', '#d5fff1': '#167d95', '#577185': '#a5b6c8', '#192231': '#edf2f8'}

def stylesheet(theme):
    css = DARK_STYLE
    if theme == 'light':
        css = re.sub(r'#[0-9a-fA-F]{6}', lambda m: LIGHT_COLORS.get(m.group(0), m.group(0)), css)
    foreground, background = ('#202b3b', '#ffffff') if theme == 'light' else ('#e8edf5', '#151d29')
    css += f"\nQMenu, QMenuBar, QToolTip {{ color: {foreground}; background: {background}; }}"
    css += "\nQPlainTextEdit#inputText { font-family: 'Segoe UI', '.AppleSystemUIFont', 'Arial'; font-size: 13px; }"
    return css
