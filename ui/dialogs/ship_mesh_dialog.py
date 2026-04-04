"""Explorer mesh Ship-to-App dialog.

Lets users package edited OBJ files as a standalone ZIP installer that
patches the correct PAZ/PAMT/PAPGT files for end users.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.mesh_ship_builder import (
    MeshShipRequest,
    build_mesh_ship_package,
    default_mesh_ship_mod_name,
    write_mesh_ship_zip,
)
from core.pamt_parser import PamtFileEntry


class ShipMeshDialog(QDialog):
    """Generate a distributable mesh-mod ZIP from edited OBJ files."""

    def __init__(
        self,
        vfs,
        config,
        entries: list[PamtFileEntry],
        prefilled_obj_paths: dict[str, str] | None = None,
        item_index=None,
        parent=None,
    ):
        super().__init__(parent)
        self._vfs = vfs
        self._config = config
        self._entries = self._unique_entries(entries)
        self._prefilled_obj_paths = {
            key.lower(): value for key, value in (prefilled_obj_paths or {}).items()
        }
        self._item_index = item_index
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("Ship Mesh Mod to App - Generate ZIP Package")
        self.setMinimumWidth(980)
        self.setMinimumHeight(560)

        layout = QVBoxLayout(self)

        info_group = QGroupBox("Mod Information")
        info_form = QFormLayout(info_group)

        self._mod_name = QLineEdit(self._default_mod_name())
        self._mod_name.setToolTip("Name shown in the installer window and README.")
        self._mod_name.setReadOnly(False)
        self._mod_name.setEnabled(True)
        self._mod_name.setClearButtonEnabled(True)
        self._mod_name.setFocusPolicy(Qt.StrongFocus)
        info_form.addRow("Mod Name:", self._mod_name)

        self._author = QLineEdit(self._config.get("explorer.mesh_ship.author", ""))
        self._author.setPlaceholderText("Your name, studio, or team")
        self._author.setReadOnly(False)
        self._author.setEnabled(True)
        self._author.setClearButtonEnabled(True)
        self._author.setFocusPolicy(Qt.StrongFocus)
        info_form.addRow("Author:", self._author)

        self._version = QLineEdit(self._config.get("explorer.mesh_ship.version", "1.0.0"))
        self._version.setReadOnly(False)
        self._version.setEnabled(True)
        self._version.setClearButtonEnabled(True)
        self._version.setFocusPolicy(Qt.StrongFocus)
        info_form.addRow("Version:", self._version)

        layout.addWidget(info_group)

        options_group = QGroupBox("Packaging Options")
        options_layout = QVBoxLayout(options_group)

        self._include_paired_lod = QCheckBox("Auto-include paired .pamlod when shipping a .pam mesh")
        self._include_paired_lod.setChecked(
            bool(self._config.get("explorer.mesh_ship.include_paired_lod", True))
        )
        self._include_paired_lod.setToolTip(
            "When enabled, a selected .pam will also generate and ship its matching "
            ".pamlod if one exists and was not explicitly selected."
        )
        options_layout.addWidget(self._include_paired_lod)

        note = QLabel(
            "The generated ZIP includes patched PAZ/PAMT/PAPGT files, install.bat, "
            "uninstall.bat, README.txt, and manifest.json for distribution."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #89b4fa;")
        options_layout.addWidget(note)

        layout.addWidget(options_group)

        assets_group = QGroupBox("Mesh Assets")
        assets_layout = QVBoxLayout(assets_group)

        self._summary = QLabel("")
        self._summary.setStyleSheet("color: #a6e3a1; font-weight: 600;")
        assets_layout.addWidget(self._summary)

        self._table = QTableWidget(len(self._entries), 6)
        self._table.setHorizontalHeaderLabels(
            ["Asset", "Group", "Format", "Edited OBJ", "Browse", "Status"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        for row, entry in enumerate(self._entries):
            group_key = os.path.basename(os.path.dirname(entry.paz_file))
            fmt = os.path.splitext(entry.path)[1].lower().lstrip(".")
            prefilled = self._prefilled_obj_paths.get(entry.path.lower(), "")

            self._table.setItem(row, 0, self._readonly_item(entry.path))
            self._table.setItem(row, 1, self._readonly_item(group_key))
            self._table.setItem(row, 2, self._readonly_item(fmt))
            self._table.setItem(row, 3, QTableWidgetItem(prefilled))

            browse_btn = QPushButton("Browse...")
            browse_btn.clicked.connect(lambda _=False, r=row: self._browse_obj_for_row(r))
            self._table.setCellWidget(row, 4, browse_btn)
            self._table.setItem(row, 5, self._readonly_item(""))
            self._update_row_status(row)

        self._table.itemChanged.connect(self._on_table_item_changed)

        assets_layout.addWidget(self._table, 1)
        layout.addWidget(assets_group, 1)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self._generate_btn = QPushButton("Generate ZIP")
        self._generate_btn.setObjectName("primary")
        self._generate_btn.clicked.connect(self._do_generate)
        btn_row.addWidget(self._generate_btn)

        layout.addLayout(btn_row)
        self._refresh_summary()

    def _readonly_item(self, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        return item

    def _default_mod_name(self) -> str:
        display_names = []
        seen = set()
        for entry in self._entries:
            for name in self._display_names_for_entry(entry):
                key = name.casefold()
                if key in seen:
                    continue
                seen.add(key)
                display_names.append(name)

        if len(display_names) == 1:
            return f"Crimson Desert - {display_names[0]} Mesh Mod"
        return default_mesh_ship_mod_name(self._entries)

    def _display_names_for_entry(self, entry: PamtFileEntry) -> list[str]:
        if not self._item_index or not getattr(self._item_index, "pac_to_items", None):
            return []

        base = os.path.splitext(os.path.basename(entry.path.lower()))[0]
        pac_name = base + ".pac"
        items = self._item_index.pac_to_items.get(pac_name, [])
        names = []
        seen = set()
        for item in items:
            name = (item.display_name or item.internal_name or "").strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        return names

    def _unique_entries(self, entries: list[PamtFileEntry]) -> list[PamtFileEntry]:
        result: list[PamtFileEntry] = []
        seen: set[str] = set()
        for entry in entries:
            key = entry.path.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(entry)
        result.sort(key=lambda item: item.path.lower())
        return result

    def _browse_obj_for_row(self, row: int) -> None:
        current = self._table.item(row, 3).text().strip()
        start_dir = os.path.dirname(current) if current else self._config.get(
            "explorer.mesh_ship.last_obj_dir",
            os.path.expanduser("~/Desktop"),
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Edited OBJ",
            start_dir,
            "OBJ Files (*.obj);;All Files (*.*)",
        )
        if not path:
            return
        self._table.item(row, 3).setText(path)
        self._config.set("explorer.mesh_ship.last_obj_dir", os.path.dirname(path))
        self._update_row_status(row)
        self._refresh_summary()

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 3:
            return
        self._update_row_status(item.row())
        self._refresh_summary()

    def _update_row_status(self, row: int) -> None:
        obj_item = self._table.item(row, 3)
        status_item = self._table.item(row, 5)
        if obj_item is None or status_item is None:
            return
        path = obj_item.text().strip()
        if not path:
            status_item.setText("Select OBJ")
            status_item.setForeground(Qt.gray)
            return
        if os.path.isfile(path):
            status_item.setText("Ready")
            status_item.setForeground(Qt.darkGreen)
            return
        status_item.setText("Missing file")
        status_item.setForeground(Qt.red)

    def _refresh_summary(self) -> None:
        ready = 0
        for row in range(self._table.rowCount()):
            status = self._table.item(row, 5).text()
            if status == "Ready":
                ready += 1
        total = self._table.rowCount()
        self._summary.setText(f"Ready assets: {ready} / {total}")

    def _collect_requests(self) -> list[MeshShipRequest]:
        requests: list[MeshShipRequest] = []
        missing: list[str] = []
        for row, entry in enumerate(self._entries):
            obj_path = self._table.item(row, 3).text().strip()
            if not obj_path or not os.path.isfile(obj_path):
                missing.append(entry.path)
                continue
            package_group = os.path.basename(os.path.dirname(entry.paz_file))
            requests.append(
                MeshShipRequest(
                    entry=entry,
                    package_group=package_group,
                    obj_path=obj_path,
                )
            )
        if missing:
            missing_text = "\n".join(missing[:8])
            if len(missing) > 8:
                missing_text += f"\n... and {len(missing) - 8} more"
            raise ValueError(
                "Select a valid edited OBJ for every listed asset before generating.\n\n"
                f"Missing:\n{missing_text}"
            )
        return requests

    def _do_generate(self) -> None:
        mod_name = self._mod_name.text().strip()
        author = self._author.text().strip()
        version = self._version.text().strip()

        if not mod_name:
            QMessageBox.warning(self, "Missing", "Enter a mod name.")
            return
        if not author:
            QMessageBox.warning(self, "Missing", "Enter an author, studio, or team name.")
            return
        if not version:
            QMessageBox.warning(self, "Missing", "Enter a version.")
            return

        try:
            requests = self._collect_requests()
        except Exception as exc:
            QMessageBox.warning(self, "Missing OBJ", str(exc))
            return

        default_name = mod_name.replace(" ", "_").replace("-", "_")
        start_dir = self._config.get(
            "explorer.mesh_ship.last_output_dir",
            os.path.expanduser("~/Desktop"),
        )
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Mesh Mod ZIP",
            os.path.join(start_dir, f"{default_name}.zip"),
            "ZIP Files (*.zip)",
        )
        if not save_path:
            return
        if not save_path.lower().endswith(".zip"):
            save_path += ".zip"

        self._config.set("explorer.mesh_ship.author", author)
        self._config.set("explorer.mesh_ship.version", version)
        self._config.set(
            "explorer.mesh_ship.include_paired_lod",
            self._include_paired_lod.isChecked(),
        )
        self._config.set("explorer.mesh_ship.last_output_dir", os.path.dirname(save_path))
        self._config.save()

        self._generate_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setValue(0)

        try:
            package = build_mesh_ship_package(
                self._vfs,
                requests,
                mod_name=mod_name,
                author=author,
                version=version,
                include_paired_lod=self._include_paired_lod.isChecked(),
                progress_callback=self._on_progress,
            )
            self._progress.setValue(92)
            self._status.setText("Writing ZIP package...")
            write_mesh_ship_zip(save_path, package, mod_name, author, version)
            self._progress.setValue(100)
            self._status.setText("Mesh package ready.")
            QMessageBox.information(
                self,
                "Done",
                f"ZIP saved to:\n{save_path}\n\n"
                f"Assets: {package.manifest['asset_count']}\n"
                f"Patched archive files: {package.manifest['archive_file_count']}\n\n"
                "End users can extract the ZIP and run install.bat.",
            )
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Mesh Ship Error", str(exc))
        finally:
            self._generate_btn.setEnabled(True)
            self._progress.setVisible(False)

    def _on_progress(self, pct: int, message: str) -> None:
        self._progress.setValue(max(0, min(100, pct)))
        self._status.setText(message)
