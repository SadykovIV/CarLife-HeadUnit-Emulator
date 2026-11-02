"""CarLife instrument display. Only the active session supplies the values."""
import struct

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)


# CarLife nextTurn values used by Baidu and HeadSwap (AaMeta.T_*).
TURNS = {
    1: ('↑', 'Прямо'), 2: ('↗', 'Плавно направо'), 3: ('↱', 'Направо'),
    4: ('↘', 'Резко направо'), 5: ('↶', 'Разворот'), 6: ('↙', 'Резко налево'),
    7: ('↰', 'Налево'), 8: ('↖', 'Плавно налево'), 9: ('⟲', 'Въезд на круг'),
    10: ('↗', 'Выезд с круга'), 11: ('↖', 'Съезд слева'), 12: ('↗', 'Съезд справа'),
    13: ('↖', 'Слияние слева'), 14: ('↖', 'Держитесь левее'),
    15: ('↗', 'Слияние справа'), 16: ('↗', 'Держитесь правее'), 17: ('↑', 'Средняя ветвь'),
    18: ('↖', 'Въезд слева'), 19: ('↗', 'Въезд справа'), 20: ('↖', 'Левая ветвь'),
    21: ('↗', 'Правая ветвь'), 23: ('●', 'Старт'), 24: ('⚑', 'Пункт назначения'),
    29: ('≈', 'Паром'),
}


def distance_text(meters):
    if meters is None:
        return '—'
    if meters < 1000:
        return f'{meters} м'
    return f'{meters / 1000:.1f}'.replace('.', ',') + ' км'


def turn_pixmap(blob):
    # HeadSwap sends PNG. Inspect dimensions before asking Qt to allocate pixels.
    if (not isinstance(blob, bytes) or not 24 <= len(blob) <= 65535 or
            blob[:8] != b'\x89PNG\r\n\x1a\n' or blob[8:16] != b'\0\0\0\rIHDR'):
        return None
    width, height = struct.unpack('>II', blob[16:24])
    if not (1 <= width <= 512 and 1 <= height <= 512):
        return None
    image = QImage.fromData(blob, 'PNG')
    if image.isNull():
        return None
    return QPixmap.fromImage(image).scaled(100, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def text_label(text, style, *, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.PlainText)
    widget.setObjectName(style)
    widget.setWordWrap(wrap)
    if wrap:
        widget.setMinimumWidth(0)
        widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    return widget


def card(title):
    frame = QFrame()
    frame.setObjectName('clusterCard')
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(28, 24, 28, 24)
    layout.setSpacing(12)
    layout.addWidget(text_label(title, 'clusterKicker'))
    return frame, layout


def waiting_text(subscription, *, music):
    state = subscription.get('state')
    if state == 'unsupported':
        return 'Телефон не поддерживает подписку на музыку.' if music else 'Телефон не поддерживает подписку на навигацию.'
    if state == 'active':
        return 'Ожидание данных о песне' if music else 'Ожидание следующего манёвра'
    if state == 'requested':
        return 'Ожидание ответа телефона на подписку'
    return 'Нет данных'


class ClusterPage(QWidget):
    def __init__(self, start_demo):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 20, 32, 24)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        heading.addWidget(text_label('Приборка', 'clusterHeading'))
        heading.addStretch()
        self.source = text_label('Отключено', 'badge')
        heading.addWidget(self.source)
        layout.addLayout(heading)
        self.hint = text_label('', 'muted', wrap=True)
        layout.addWidget(self.hint)

        grid = QGridLayout()
        grid.setSpacing(16)
        nav, box = card('СЛЕДУЮЩИЙ МАНЁВР')
        self.arrow = text_label('—', 'clusterArrow')
        self.arrow.setAlignment(Qt.AlignCenter)
        self.arrow.setFixedSize(116, 116)
        self.arrow.setAccessibleName('Стрелка манёвра')
        box.addWidget(self.arrow)
        self.distance = text_label('—', 'clusterValue')
        box.addWidget(self.distance)
        self.maneuver = text_label('', 'clusterTitle', wrap=True)
        box.addWidget(self.maneuver)
        self.road = text_label('', 'clusterSecondary', wrap=True)
        box.addWidget(self.road)
        box.addStretch()
        grid.addWidget(nav, 0, 0)
        music, box = card('СЕЙЧАС ИГРАЕТ')
        box.addStretch()
        self.song = text_label('—', 'clusterTitle', wrap=True)
        box.addWidget(self.song)
        self.artist = text_label('', 'clusterSecondary', wrap=True)
        box.addWidget(self.artist)
        self.album = text_label('', 'muted', wrap=True)
        box.addWidget(self.album)
        box.addStretch()
        grid.addWidget(music, 0, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        layout.addLayout(grid, 1)
        self.demo_button = QPushButton('Запустить демо приборки')
        self.demo_button.clicked.connect(start_demo)
        layout.addWidget(self.demo_button, 0, Qt.AlignLeft)
        self._rendered = None
        self._icon = None
        self.refresh(None, demo=False)

    def refresh(self, cluster, *, demo):
        snapshot = cluster.snapshot() if cluster is not None else {}
        key = (cluster, snapshot.get('revision'), demo)
        if key == self._rendered:
            return
        self._rendered = key
        self.source.setText('  Демо  ' if demo else ('  CarLife  ' if cluster is not None else '  Отключено  '))
        self.hint.setText('Демонстрационный маршрут и музыка поступают от локального эмулятора телефона.'
                          if demo else ('Данные поступают от телефона по подписке CarLife.' if cluster is not None
                          else 'Подключите телефон или запустите демо, чтобы увидеть манёвры и музыку.'))
        self.demo_button.setVisible(cluster is None)
        subscriptions = snapshot.get('subscriptions', {})
        nav = snapshot.get('navigation') or {}
        music = snapshot.get('music') or {}
        turn = nav.get('turn_id')
        glyph, name = TURNS.get(turn, ('?', f'Манёвр {turn}'))
        self.maneuver.setText(name if nav else waiting_text(subscriptions.get('navigation', {}), music=False))
        self.distance.setText(distance_text(nav.get('distance_m')))
        self.road.setText(nav.get('road') or '')
        icon_key = (turn, nav.get('icon'))
        if icon_key != self._icon:
            self._icon = icon_key
            picture = turn_pixmap(nav.get('icon'))
            if picture is not None:
                self.arrow.setPixmap(picture)
            else:
                self.arrow.setText(glyph if nav else '—')
        self.song.setText(music.get('title') or (waiting_text(subscriptions.get('music', {}), music=True)
                                                 if not music else 'Без названия'))
        self.artist.setText(music.get('artist') or '')
        self.album.setText(music.get('album') or '')
