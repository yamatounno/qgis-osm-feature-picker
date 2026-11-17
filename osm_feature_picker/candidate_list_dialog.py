from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

# Qt aborts the process if a QThread is destroyed while still running. A
# worker that doesn't stop within WORKER_STOP_TIMEOUT_MS of being asked to
# (e.g. stuck mid-request) is detached from the dialog and kept alive here
# instead, so it can finish on its own without crashing anything.
WORKER_STOP_TIMEOUT_MS = 10000
_ORPHANED_WORKERS = []


class _DescriptionWorker(QThread):
    """Fetches descriptions for a list of items one at a time in the
    background, emitting each result as it arrives so the UI thread is
    never blocked on network I/O."""

    described = pyqtSignal(str, str)

    def __init__(self, items, provider, parent=None):
        super().__init__(parent)
        self._items = items
        self._provider = provider
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        for item in self._items:
            if self._stop:
                return
            try:
                description = self._provider(item) or ""
            except Exception:
                description = ""
            if self._stop:
                return
            self.described.emit(item, description)


class CandidateListDialog(QDialog):
    """Shows (item, count) pairs found within a bbox and lets the user pick
    one. Used both for tag keys and for the values of a given key.

    If `description_provider` is given, a "説明文" column is added and
    filled in progressively in the background (via `_DescriptionWorker`)
    without blocking the UI. `description_cache` (item -> description),
    when given, is read for already-known descriptions and mutated in
    place as new ones are fetched, so callers can keep it across dialog
    instances to avoid re-fetching."""

    def __init__(
        self,
        rows,
        title,
        column_label,
        parent=None,
        description_provider=None,
        description_cache=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(360, 480)
        self.selected_item = None
        self._all_rows = rows
        self._column_label = column_label
        self._description_provider = description_provider
        self._description_cache = description_cache if description_cache is not None else {}
        self._show_descriptions = description_provider is not None
        self._worker = None

        layout = QVBoxLayout(self)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(f"{column_label}を絞り込み...")
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)

        column_count = 3 if self._show_descriptions else 2
        self.table = QTableWidget(0, column_count)
        headers = [column_label, "件数"] + (["説明文"] if self._show_descriptions else [])
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        if self._show_descriptions:
            self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        else:
            self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self.table)

        self._populate(rows)

        if self._show_descriptions:
            self._start_description_worker()

        button_layout = QHBoxLayout()
        add_button = QPushButton(f"選択した{column_label}をタグフィルタに追加")
        add_button.clicked.connect(self._on_add_clicked)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.reject)
        button_layout.addWidget(add_button)
        button_layout.addStretch(1)
        button_layout.addWidget(close_button)
        layout.addLayout(button_layout)

    def _populate(self, rows):
        self.table.setRowCount(0)
        for item_text, count in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)

            item_cell = QTableWidgetItem(item_text)
            self.table.setItem(row, 0, item_cell)

            count_item = QTableWidgetItem(str(count))
            count_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, 1, count_item)

            if self._show_descriptions:
                description = self._description_cache.get(item_text, "")
                desc_item = QTableWidgetItem(description)
                self.table.setItem(row, 2, desc_item)
                if description:
                    item_cell.setToolTip(description)
                    desc_item.setToolTip(description)

    def _apply_filter(self, text):
        text = text.strip().lower()
        rows = [row for row in self._all_rows if text in row[0].lower()] if text else self._all_rows
        self._populate(rows)

    def _on_row_double_clicked(self, row, _column):
        self.selected_item = self.table.item(row, 0).text()
        self.accept()

    def _on_add_clicked(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.information(self, "未選択", f"追加する{self._column_label}を選択してください。")
            return
        self.selected_item = self.table.item(selected_rows[0].row(), 0).text()
        self.accept()

    def _start_description_worker(self):
        pending = [item for item, _ in self._all_rows if item not in self._description_cache]
        if not pending:
            return
        self._worker = _DescriptionWorker(pending, self._description_provider, self)
        self._worker.described.connect(self._on_description_ready)
        self._worker.start()

    def _on_description_ready(self, item_text, description):
        self._description_cache[item_text] = description
        if not description:
            return
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, 0)
            if cell is not None and cell.text() == item_text:
                cell.setToolTip(description)
                desc_item = self.table.item(row, 2)
                if desc_item is not None:
                    desc_item.setText(description)
                    desc_item.setToolTip(description)
                break

    def _stop_worker(self):
        if self._worker is None:
            return
        worker = self._worker
        self._worker = None

        try:
            worker.described.disconnect(self._on_description_ready)
        except TypeError:
            pass
        worker.request_stop()

        if worker.wait(WORKER_STOP_TIMEOUT_MS):
            return

        # Still running after the grace period (e.g. a hung request). Detach
        # it from this dialog so Qt's parent-child cleanup on close doesn't
        # destroy a live QThread (which would abort the process), and keep a
        # reference until it finishes naturally in the background.
        worker.setParent(None)
        _ORPHANED_WORKERS.append(worker)

        def _forget():
            if worker in _ORPHANED_WORKERS:
                _ORPHANED_WORKERS.remove(worker)

        worker.finished.connect(_forget)

    def closeEvent(self, event):
        self._stop_worker()
        super().closeEvent(event)

    def accept(self):
        self._stop_worker()
        super().accept()

    def reject(self):
        self._stop_worker()
        super().reject()
