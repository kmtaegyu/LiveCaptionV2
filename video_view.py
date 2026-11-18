"""Render video and subtitles in the same scene for reliable stacking."""
from functools import lru_cache

from PySide6.QtCore import Qt, QSizeF, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QLabel, QFrame, QSizePolicy

LINE_WIDTH_RATIO = 0.88      # a caption line never spans more than this share of the view
MIN_FONT_SCALE = 0.75        # shrink at most this far before adding a third line


def wrap_caption(text, measure, max_width, max_lines=2):
    """Split ``text`` at spaces into at most ``max_lines`` balanced lines.

    Balanced means the longest line is as short as possible, like broadcast
    captions, rather than a full first line and a stub. Returns None when even
    ``max_lines`` lines cannot fit ``max_width``.
    """
    if measure(text) <= max_width:
        return [text]
    words = text.split(" ")

    @lru_cache(maxsize=None)
    def best(start, lines_left):
        rest = " ".join(words[start:])
        if lines_left == 1:
            return measure(rest), (rest,)
        choice = (measure(rest), (rest,))
        for end in range(start + 1, len(words)):
            head = " ".join(words[start:end])
            head_width = measure(head)
            if head_width > max_width:
                break
            tail_width, tail = best(end, lines_left - 1)
            width = max(head_width, tail_width)
            if width < choice[0]:
                choice = (width, (head, *tail))
        return choice

    width, lines = best(0, max_lines)
    return list(lines) if width <= max_width else None


def wrap_by_characters(text, measure, max_width):
    """Last resort for a single word wider than the view: break anywhere."""
    lines, line = [], ""
    for char in text:
        if line and measure(line + char) > max_width:
            lines.append(line.rstrip())
            line = char.lstrip()
        else:
            line += char
    return lines + [line] if line else lines


class OutlinedSubtitleLabel(QLabel):
    """Paint a real glyph outline rather than a background or offset shadow."""

    fill = QColor("white")
    lines = ()

    def paintEvent(self, event):
        lines = self.lines or ([self.text()] if self.text() else [])
        if not lines:
            return
        metrics = QFontMetrics(self.font())
        step = metrics.lineSpacing()
        top = (self.height() - (step * (len(lines) - 1) + metrics.height())) / 2
        path = QPainterPath()
        for index, line in enumerate(lines):
            x = (self.width() - metrics.horizontalAdvance(line)) / 2
            path.addText(x, top + index * step + metrics.ascent(), self.font(), line)
        path.setFillRule(Qt.FillRule.WindingFill)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(Qt.GlobalColor.black, max(3.0, self.font().pixelSize() / 7))
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.fillPath(path, self.fill)


class VideoView(QGraphicsView):
    mouse_moved = Signal()
    double_clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self.setMinimumSize(240, 135)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.base_font_size = 22
        self.subtitles_visible = True
        self.video_item = QGraphicsVideoItem()
        self.video_item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.scene().addItem(self.video_item)
        self.subtitle_label = OutlinedSubtitleLabel()
        self.subtitle_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.subtitle_label.setTextFormat(Qt.TextFormat.PlainText)
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle_label.setWordWrap(False)
        self.subtitle_label.setStyleSheet(
            "background-color: transparent; color: white; "
            "font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; "
            "padding: 8px;"
        )
        self.subtitle_item = self.scene().addWidget(self.subtitle_label)
        self.subtitle_item.setZValue(1)
        self.subtitle_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.subtitle_item.setVisible(False)
        self.placeholder = QLabel("파일을 열거나 YouTube 라이브·동영상 URL을 붙여넣으세요", self)
        self.placeholder.setObjectName("placeholder")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_subtitle(self, text, color="white", interim=False):
        """Show one caption; interim (still being recognized) text is dimmed."""
        text = " ".join(text.split())
        fill = QColor(color)
        fill.setAlphaF(0.72 if interim else 1.0)
        self.subtitle_label.fill = fill
        self.subtitle_label.setText(text)
        self.subtitle_label.update()
        self.subtitle_item.setVisible(bool(text) and self.subtitles_visible)
        self.update_geometry()

    def set_subtitles_visible(self, visible):
        self.subtitles_visible = visible
        self.subtitle_item.setVisible(bool(self.subtitle_label.text()) and visible)

    def set_font_size(self, pixels):
        self.base_font_size = pixels
        self.update_geometry()

    def layout_caption(self, text, base, max_width):
        """Pick a font size and line breaks: two balanced lines first, then smaller, then three."""
        font = self.subtitle_label.font()
        if not text:
            font.setPixelSize(base)
            return font, []
        floor = max(1, round(base * MIN_FONT_SCALE))
        # Two lines down to 75 % size, then three lines at any size, so the caption never
        # grows taller than three lines over the picture.
        for max_lines, sizes in ((2, range(base, floor - 1, -1)), (3, range(base, 0, -1))):
            for size in sizes:
                font.setPixelSize(size)
                metrics = QFontMetrics(font)
                lines = wrap_caption(text, metrics.horizontalAdvance, max_width, max_lines)
                if lines:
                    return font, lines
        metrics = QFontMetrics(font)
        return font, wrap_by_characters(text, metrics.horizontalAdvance, max_width)

    def update_geometry(self):
        size = self.viewport().size()
        self.setSceneRect(0, 0, size.width(), size.height())
        self.video_item.setSize(QSizeF(size))
        self.placeholder.setGeometry(0, 0, size.width(), size.height())
        width = max(1, size.width() - 48)
        # Grow with the view so captions stay readable in full screen.
        base = round(self.base_font_size * max(1.0, size.height() / 540))
        max_width = max(1, min(width - 16, round(size.width() * LINE_WIDTH_RATIO)))
        font, lines = self.layout_caption(self.subtitle_label.text(), base, max_width)
        self.subtitle_label.setFont(font)
        self.subtitle_label.lines = tuple(lines)
        metrics = QFontMetrics(font)
        height = metrics.height() + metrics.lineSpacing() * max(0, len(lines) - 1) + 16
        self.subtitle_item.setMinimumSize(width, height)
        self.subtitle_item.setMaximumSize(width, height)
        self.subtitle_item.resize(width, height)
        self.subtitle_item.setPos(24, max(0, size.height() - height - 24))
        self.subtitle_label.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "subtitle_item"):
            self.update_geometry()

    def mouseMoveEvent(self, event):
        self.mouse_moved.emit()
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()
