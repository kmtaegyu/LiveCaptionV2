"""Dark theme stylesheet and painted icons shared by the player widgets."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF


TEXT = "#e6e8ec"
MUTED = "#8b93a1"
ACCENT = "#4c8dff"

# Caption colours per speaker: 화자 1 white, then yellow, cyan, green, pink, violet.
SPEAKER_COLORS = ["#ffffff", "#ffe066", "#7fdbff", "#9be564", "#ff9ecd", "#c9a7ff"]


def speaker_color(name, default="#ffffff"):
    """Stable colour for a speaker label such as '화자 2' (or any enrolled name)."""
    if not name:
        return default
    if name.startswith("화자 ") and name[3:].isdigit():
        index = int(name[3:]) - 1
    else:
        index = sum(map(ord, name))
    return SPEAKER_COLORS[index % len(SPEAKER_COLORS)]


STYLE = """
QMainWindow, QDockWidget, QWidget#root { background: #15171c; }
QWidget { color: #e6e8ec; font-family: 'Malgun Gothic', '맑은 고딕', 'Segoe UI', sans-serif; font-size: 13px; }
QFrame#topBar, QFrame#controlBar { background: #1b1e25; }
QFrame#topBar { border-bottom: 1px solid #2c313c; }
QFrame#controlBar { border-top: 1px solid #2c313c; }
QMenuBar { background: #1b1e25; }
QMenuBar::item { padding: 4px 10px; background: transparent; }
QMenuBar::item:selected, QMenu::item:selected { background: #2f3542; }
QMenu { background: #1e2128; border: 1px solid #2c313c; }
QMenu::item { padding: 5px 24px; }
QStatusBar { background: #1b1e25; color: #8b93a1; border-top: 1px solid #2c313c; }
QStatusBar QLabel { color: #8b93a1; padding: 0 6px; }
QLineEdit, QPlainTextEdit, QListWidget {
    background: #101216; border: 1px solid #2c313c; border-radius: 6px; padding: 5px 8px;
    selection-background-color: #4c8dff; selection-color: white; }
QLineEdit:focus { border-color: #4c8dff; }
QSpinBox, QDoubleSpinBox { min-height: 24px; }
QPushButton { background: #2a2f3a; border: 1px solid #353b48; border-radius: 6px; padding: 5px 12px; }
QPushButton:hover { background: #343a47; }
QPushButton:pressed { background: #23272f; }
QPushButton:disabled { color: #5b6270; background: #20232a; border-color: #272b34; }
QPushButton:checked { background: #26406f; border-color: #4c8dff; }
QPushButton#primary { background: #4c8dff; border-color: #4c8dff; color: white; font-weight: bold; }
QPushButton#primary:hover { background: #659dff; }
QToolButton { background: transparent; border: none; border-radius: 6px; padding: 6px; }
QToolButton:hover { background: #2f3542; }
QToolButton:checked { background: #26406f; }
QToolButton:disabled { background: transparent; }
QSlider::groove:horizontal { height: 4px; background: #343a47; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #4c8dff; border-radius: 2px; }
QSlider::sub-page:horizontal:disabled { background: #454b58; }
QSlider::handle:horizontal { background: #e6e8ec; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }
QSlider::handle:horizontal:disabled { background: #5b6270; }
QLabel#time { color: #c4c9d2; font-family: 'Consolas', monospace; }
QLabel#badge { background: #d93a3a; color: white; border-radius: 4px; padding: 2px 7px; font-weight: bold; font-size: 11px; }
QLabel#badge[delayed="true"] { background: #a8741a; }
QLabel#hint { color: #8b93a1; font-size: 12px; }
QLabel#offset { font-size: 20px; font-weight: bold; }
QLabel#placeholder { color: #6f7785; font-size: 15px; background: transparent; }
QGroupBox { border: 1px solid #2c313c; border-radius: 8px; margin-top: 10px; padding: 10px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #8b93a1; }
QDockWidget { titlebar-close-icon: none; }
QComboBox { background: #2a2f3a; border: 1px solid #353b48; border-radius: 6px; padding: 4px 8px; min-height: 22px; }
QComboBox QAbstractItemView { background: #1e2128; border: 1px solid #2c313c; selection-background-color: #26406f; }
QToolButton#sectionToggle, QDockWidget QToolButton { color: #8b93a1; }
QDockWidget::title { background: #1b1e25; padding: 6px 10px; border-bottom: 1px solid #2c313c; }
QListWidget { padding: 2px; }
QListWidget::item { padding: 4px 6px; border-radius: 4px; }
QListWidget::item:selected { background: #26406f; color: white; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #454b58; border-radius: 4px; background: #101216; }
QCheckBox::indicator:checked { background: #4c8dff; border-color: #4c8dff; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #3a404d; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: #1e2128; color: #e6e8ec; border: 1px solid #2c313c; padding: 4px; }
"""


def dark_palette():
    """Palette for widgets Fusion draws itself (spin boxes), matching the stylesheet."""
    palette = QPalette()
    roles = QPalette.ColorRole
    for role, color in ((roles.Window, "#15171c"), (roles.Base, "#101216"), (roles.AlternateBase, "#1b1e25"),
                        (roles.Button, "#2a2f3a"), (roles.Text, TEXT), (roles.WindowText, TEXT),
                        (roles.ButtonText, TEXT), (roles.PlaceholderText, "#6f7785"),
                        (roles.Highlight, ACCENT), (roles.HighlightedText, "#ffffff"),
                        (roles.ToolTipBase, "#1e2128"), (roles.ToolTipText, TEXT)):
        palette.setColor(role, QColor(color))
    for role in (roles.Text, roles.WindowText, roles.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#5b6270"))
    return palette


def make_icon(name, color=TEXT, size=20):
    """Paint a simple glyph so icons look the same on every Windows theme."""
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 20, size / 20)
    fill = QColor(color)
    stroke = QPen(fill, 1.8)
    stroke.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroke.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    if name == "play":
        painter.drawPolygon(QPolygonF([QPointF(6, 3.5), QPointF(16.5, 10), QPointF(6, 16.5)]))
    elif name == "pause":
        painter.drawRoundedRect(QRectF(4.5, 4, 4, 12), 1, 1)
        painter.drawRoundedRect(QRectF(11.5, 4, 4, 12), 1, 1)
    elif name == "stop":
        painter.drawRoundedRect(QRectF(5, 5, 10, 10), 1.5, 1.5)
    elif name in ("volume", "mute"):
        painter.drawPolygon(QPolygonF([QPointF(2.5, 7.5), QPointF(5.5, 7.5), QPointF(9.5, 4),
                                       QPointF(9.5, 16), QPointF(5.5, 12.5), QPointF(2.5, 12.5)]))
        painter.setPen(stroke)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if name == "volume":
            painter.drawArc(QRectF(8.5, 6.5, 7, 7), -55 * 16, 110 * 16)
            painter.drawArc(QRectF(6.5, 3.5, 12, 13), -50 * 16, 100 * 16)
        else:
            painter.drawLine(QPointF(12.5, 7.5), QPointF(17.5, 12.5))
            painter.drawLine(QPointF(17.5, 7.5), QPointF(12.5, 12.5))
    elif name == "fullscreen":
        painter.setPen(stroke)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for x, y, dx, dy in ((3.5, 3.5, 1, 1), (16.5, 3.5, -1, 1), (3.5, 16.5, 1, -1), (16.5, 16.5, -1, -1)):
            painter.drawPolyline(QPolygonF([QPointF(x + 4.5 * dx, y), QPointF(x, y), QPointF(x, y + 4.5 * dy)]))
    elif name == "cc":
        painter.setPen(stroke)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(2, 4, 16, 12), 2.5, 2.5)
        font = QFont("Arial")
        font.setPixelSize(7)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(2, 4, 16, 12), Qt.AlignmentFlag.AlignCenter, "CC")
    elif name == "panel":
        painter.setPen(stroke)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(2.5, 4, 15, 12), 2, 2)
        painter.drawLine(QPointF(12, 4), QPointF(12, 16))
    painter.end()
    return QIcon(pixmap)
