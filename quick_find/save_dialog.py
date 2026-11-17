"""The Save window: name, remarks, colour and line width / marker size.

Rows the target layer has no column for are left out. (The same file is in
Quick Find and Route Drawer, so each plugin installs on its own.)
"""
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                                 QLabel, QLineEdit, QPlainTextEdit, QVBoxLayout)
from qgis.gui import QgsColorButton

# column names a layer may already use for remarks / colour
REMARKS_COLUMNS = ("remarks", "remark", "notes", "note", "comments", "comment")
COLOR_COLUMNS = ("color", "colour", "kml_color")


def find_column(layer, candidates):
    """Index of the first column named like one of candidates (any letter case), else -1."""
    names = [n.lower() for n in layer.fields().names()]
    return next((names.index(c) for c in candidates if c in names), -1)


class SaveDialog(QDialog):
    def __init__(self, parent, title, name, remarks=True, color=None, size=None,
                 size_label="Width", size_range=(0.2, 5.0), note=""):
        """remarks False / color None / size None leave that row out."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(name)
        self.name_edit.selectAll()
        form.addRow("Name", self.name_edit)
        self.remarks_edit = None
        if remarks:
            self.remarks_edit = QPlainTextEdit()
            self.remarks_edit.setPlaceholderText("Optional, e.g. survey notes, pole numbers, contact")
            self.remarks_edit.setFixedHeight(76)
            self.remarks_edit.setTabChangesFocus(True)
            form.addRow("Remarks", self.remarks_edit)
        self.color_button = None
        if color is not None:
            self.color_button = QgsColorButton()
            self.color_button.setColor(QColor(color))
            self.color_button.setAllowOpacity(False)
            self.color_button.setShowNoColor(False)
            self.color_button.setColorDialogTitle(f"{title}: colour")
            self.color_button.setMinimumWidth(140)
            form.addRow("Colour", self.color_button)
        self.size_spin = None
        if size is not None:
            self.size_spin = QDoubleSpinBox()
            self.size_spin.setRange(*size_range)
            self.size_spin.setDecimals(1)
            self.size_spin.setSingleStep(0.1)
            self.size_spin.setSuffix(" mm")
            self.size_spin.setValue(float(size))
            form.addRow(size_label, self.size_spin)
        layout.addLayout(form)
        if note:
            hint = QLabel(note)
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #666;")
            layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name_edit.setFocus()

    def values(self, default_name):
        return {
            "name": self.name_edit.text().strip() or default_name,
            "remarks": self.remarks_edit.toPlainText().strip() if self.remarks_edit else None,
            "color": self.color_button.color().name() if self.color_button else None,
            "size": round(self.size_spin.value(), 2) if self.size_spin else None,
        }


def ask(parent, title, default_name, **rows):
    """Show the window; the values, or None when cancelled."""
    dialog = SaveDialog(parent, title, default_name, **rows)
    return dialog.values(default_name) if dialog.exec() else None
