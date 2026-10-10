"""Developer UI tuning, magnetic-rail lab and diagnostics tools."""
from __future__ import annotations

from typing import Any
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt
from ..models import UiTunerHostPort
from ..constants import DEV_UI_COLOR_FIELDS, DEV_UI_COLOR_PROFILE_FIELDS, DEV_UI_LAYOUT_DEFAULTS, DEV_UI_LAYOUT_PRESETS, DEV_UI_SURFACE_DEFAULTS, DEV_UI_STATUS_COLOR_FIELDS, DEV_UI_STATUS_PRESETS
from ..utilities import DEV_UI_STATUS_FONT_DEFAULTS, TYPOGRAPHY_DEFAULTS, TYPOGRAPHY_ROLE_LABELS, TextRole, typography_controller, typography_font

class UiTunerDialog(QtWidgets.QWidget):
    """Live developer-only controls for visual contrast and layout geometry."""

    def __init__(self, host: UiTunerHostPort):
        super().__init__(host)
        self.host = host
        self.setObjectName('settingsEmbeddedTool')
        self._color_edits: dict[str, QtWidgets.QLineEdit] = {}
        self._color_buttons: dict[str, QtWidgets.QPushButton] = {}
        self._layout_spins: dict[str, QtWidgets.QSpinBox] = {}
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        note = QtWidgets.QLabel('Developer-only live overrides. Changes are persistent until reset; normal Nightwatch defaults are never rewritten.')
        note.setObjectName('subtleLabel')
        note.setWordWrap(True)
        root.addWidget(note)
        tabs = QtWidgets.QTabWidget()
        root.addWidget(tabs, 1)
        colors_page = QtWidgets.QWidget()
        colors_root = QtWidgets.QVBoxLayout(colors_page)
        colors_root.setContentsMargins(10, 10, 10, 10)
        colors_root.setSpacing(10)
        color_preset_row = QtWidgets.QWidget()
        color_preset_layout = QtWidgets.QFormLayout(color_preset_row)
        color_preset_layout.setContentsMargins(0, 0, 0, 0)
        color_preset_layout.setHorizontalSpacing(12)
        self.color_preset = QtWidgets.QComboBox()
        self.color_preset.addItem('Custom', 'Custom')
        self.color_preset.addItem('Theme default', 'Theme default')
        self._set_combo_data(self.color_preset, host.current_developer_ui_color_preset())
        self.color_preset.currentIndexChanged.connect(lambda _index: self._color_preset_changed())
        color_preset_layout.addRow('Color style', self.color_preset)
        colors_root.addWidget(color_preset_row)
        color_note = QtWidgets.QLabel('These are explicit color overrides. Unset semantic colors continue to derive from the selected base theme and propagate through the canonical resolver to shell, chart, rail and DOM.')
        color_note.setObjectName('subtleLabel')
        color_note.setWordWrap(True)
        colors_root.addWidget(color_note)
        colors_scroll = QtWidgets.QScrollArea()
        colors_scroll.setWidgetResizable(True)
        colors_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        colors_content = QtWidgets.QWidget()
        colors_layout = QtWidgets.QGridLayout(colors_content)
        colors_layout.setContentsMargins(0, 0, 0, 0)
        colors_layout.setHorizontalSpacing(8)
        colors_layout.setVerticalSpacing(6)
        colors_layout.setColumnStretch(1, 1)
        for row, (key, label) in enumerate(DEV_UI_COLOR_PROFILE_FIELDS):
            colors_layout.addWidget(QtWidgets.QLabel(label), row, 0)
            edit = QtWidgets.QLineEdit(host.ui_theme.get(key, '#000000'))
            edit.setMaxLength(9)
            edit.editingFinished.connect(lambda name=key, field=edit: self._commit_color(name, field.text()))
            pick = QtWidgets.QPushButton('PICK')
            pick.setFixedWidth(62)
            pick.clicked.connect(lambda _checked=False, name=key: self._pick_color(name))
            colors_layout.addWidget(edit, row, 1)
            colors_layout.addWidget(pick, row, 2)
            self._color_edits[key] = edit
            self._color_buttons[key] = pick
            self._update_color_button(key)
        colors_layout.setRowStretch(len(DEV_UI_COLOR_FIELDS), 1)
        colors_scroll.setWidget(colors_content)
        colors_root.addWidget(colors_scroll, 1)
        reset_colors_tab = QtWidgets.QPushButton('RESET TO THEME COLORS')
        reset_colors_tab.clicked.connect(self._reset_colors)
        colors_root.addWidget(reset_colors_tab, 0, Qt.AlignmentFlag.AlignRight)
        tabs.addTab(colors_page, 'COLORS')
        surfaces_page = QtWidgets.QWidget()
        surfaces_form = QtWidgets.QFormLayout(surfaces_page)
        surfaces_form.setContentsMargins(14, 14, 14, 14)
        surfaces_form.setHorizontalSpacing(16)
        surfaces_form.setVerticalSpacing(9)
        self._surface_spins: dict[str, QtWidgets.QSpinBox] = {}
        for key, label, minimum, maximum in (('block_border_width', 'Structural block border', 0, 4), ('block_radius', 'Structural block radius', 0, 16), ('element_radius', 'Element/control radius', 0, 12)):
            spin = QtWidgets.QSpinBox()
            spin.setRange(minimum, maximum)
            spin.setSuffix(' px')
            spin.setValue(int(host.developer_ui_surfaces.get(key, DEV_UI_SURFACE_DEFAULTS[key])))
            spin.setEnabled(False)
            spin.setToolTip('Locked by the current build-time surface contract.')
            surfaces_form.addRow(label, spin)
            self._surface_spins[key] = spin
        surface_note = QtWidgets.QLabel('Surface geometry is locked by the current build-time UI contract. These values are shown for reference only; color, status and typography tuning remain live.')
        surface_note.setObjectName('subtleLabel')
        surface_note.setWordWrap(True)
        surfaces_form.addRow(surface_note)
        tabs.addTab(surfaces_page, 'SURFACES')
        status_page = QtWidgets.QWidget()
        status_root = QtWidgets.QVBoxLayout(status_page)
        status_root.setContentsMargins(10, 10, 10, 10)
        status_root.setSpacing(10)
        status_scroll = QtWidgets.QScrollArea()
        status_scroll.setWidgetResizable(True)
        status_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        status_content = QtWidgets.QWidget()
        status_layout = QtWidgets.QFormLayout(status_content)
        status_layout.setHorizontalSpacing(14)
        status_layout.setVerticalSpacing(7)
        self._status_color_edits: dict[str, QtWidgets.QLineEdit] = {}
        self._status_color_buttons: dict[str, QtWidgets.QPushButton] = {}
        self._status_font_combos: dict[str, QtWidgets.QComboBox] = {}
        for key, label in DEV_UI_STATUS_COLOR_FIELDS:
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            edit = QtWidgets.QLineEdit(str(host.developer_ui_status[key]))
            edit.setMaxLength(9)
            edit.editingFinished.connect(lambda name=key, field=edit: self._commit_status_color(name, field.text()))
            pick = QtWidgets.QPushButton('PICK')
            pick.setFixedWidth(62)
            pick.clicked.connect(lambda _checked=False, name=key: self._pick_status_color(name))
            row_layout.addWidget(edit, 1)
            row_layout.addWidget(pick)
            status_layout.addRow(label, row)
            self._status_color_edits[key] = edit
            self._status_color_buttons[key] = pick
            self._update_status_color_button(key)
        self.status_density = QtWidgets.QComboBox()
        self.status_density.addItem('Custom', 'Custom')
        for name in DEV_UI_STATUS_PRESETS:
            self.status_density.addItem(name, name)
        self._set_combo_data(self.status_density, host.current_developer_ui_status_preset())
        self.status_density.currentIndexChanged.connect(lambda _index: self._status_density_changed())
        status_layout.addRow('Density', self.status_density)
        for key, default in DEV_UI_STATUS_FONT_DEFAULTS.items():
            combo = QtWidgets.QComboBox()
            for role, label in TYPOGRAPHY_ROLE_LABELS.items():
                combo.addItem(label, role)
            self._set_combo_data(combo, host.developer_ui_status.get(key, default))
            combo.currentIndexChanged.connect(lambda _index, name=key, widget=combo: host.set_developer_ui_status_value(name, widget.currentData()))
            status_layout.addRow(key.replace('_font_role', '').replace('_', ' ').title() + ' font', combo)
            self._status_font_combos[key] = combo
        status_scroll.setWidget(status_content)
        status_root.addWidget(status_scroll)
        reset_status = QtWidgets.QPushButton('RESET STATUS BAR')
        reset_status.clicked.connect(self._reset_status)
        status_root.addWidget(reset_status, 0, Qt.AlignmentFlag.AlignRight)
        tabs.addTab(status_page, 'STATUS BAR')
        geometry_page = QtWidgets.QWidget()
        geometry_root = QtWidgets.QVBoxLayout(geometry_page)
        geometry_root.setContentsMargins(0, 0, 0, 0)
        geometry_scroll = QtWidgets.QScrollArea()
        geometry_scroll.setWidgetResizable(True)
        geometry_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        geometry_content = QtWidgets.QWidget()
        geometry_layout = QtWidgets.QVBoxLayout(geometry_content)
        geometry_layout.setContentsMargins(10, 10, 10, 10)
        geometry_layout.setSpacing(10)
        geometry_lock_note = QtWidgets.QLabel('Geometry is locked by the current build-time layout contract. Controls below are reference-only so they cannot appear to apply and then snap back.')
        geometry_lock_note.setObjectName('subtleLabel')
        geometry_lock_note.setWordWrap(True)
        geometry_layout.addWidget(geometry_lock_note)
        preset_group = QtWidgets.QGroupBox('UI PRESETS')
        preset_form = QtWidgets.QFormLayout(preset_group)
        preset_form.setContentsMargins(10, 8, 10, 8)
        preset_form.setHorizontalSpacing(12)
        preset_form.setVerticalSpacing(7)
        self.layout_preset = QtWidgets.QComboBox()
        self.layout_preset.addItem('Custom', 'Custom')
        for name in DEV_UI_LAYOUT_PRESETS:
            self.layout_preset.addItem(name, name)
        self._set_combo_data(self.layout_preset, host.current_developer_ui_layout_preset())
        self.layout_preset.setEnabled(False)
        self.layout_preset.setToolTip('Locked by the current build-time layout contract.')
        preset_form.addRow('Layout', self.layout_preset)
        preset_note = QtWidgets.QLabel('Presets own the repetitive margins and padding. Near zero is the dense reference layout; the others add separation only where it helps distinguish functional groups.')
        preset_note.setObjectName('subtleLabel')
        preset_note.setWordWrap(True)
        preset_form.addRow(preset_note)
        geometry_layout.addWidget(preset_group)
        spacing_group = QtWidgets.QGroupBox('FINE TUNE')
        spacing_form = QtWidgets.QFormLayout(spacing_group)
        spacing_form.setContentsMargins(10, 8, 10, 8)
        spacing_form.setHorizontalSpacing(12)
        spacing_form.setVerticalSpacing(7)
        self.shared_control_spacing = QtWidgets.QSpinBox()
        self.shared_control_spacing.setRange(0, 12)
        self.shared_control_spacing.setSuffix(' px')
        self.shared_control_spacing.setValue(int(host.developer_ui_layout.get('toolbar_gap', 1)))
        self.shared_control_spacing.setEnabled(False)
        self.shared_control_spacing.setToolTip('Locked by the current build-time layout contract.')
        spacing_form.addRow('Control spacing', self.shared_control_spacing)
        geometry_layout.addWidget(spacing_group)
        self._add_geometry_group(geometry_layout, 'STRUCTURE', (('topbar_group_gap', 'Logical group separation', 0, 24), ('instrument_height', 'Instrument bar height', 30, 64), ('right_panel_margin', 'Right-panel padding', 0, 12), ('axis_width', 'Price-axis width', 56, 120)))
        geometry_layout.addStretch(1)
        geometry_scroll.setWidget(geometry_content)
        geometry_root.addWidget(geometry_scroll)
        tabs.addTab(geometry_page, 'GEOMETRY')
        typography_page = QtWidgets.QWidget()
        typography_root = QtWidgets.QHBoxLayout(typography_page)
        typography_root.setContentsMargins(10, 10, 10, 10)
        typography_root.setSpacing(12)
        self.typography_roles = QtWidgets.QListWidget()
        self.typography_roles.setFixedWidth(210)
        for role, label in TYPOGRAPHY_ROLE_LABELS.items():
            item = QtWidgets.QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, role)
            self.typography_roles.addItem(item)
        typography_root.addWidget(self.typography_roles)
        typography_controls = QtWidgets.QWidget()
        typography_form = QtWidgets.QFormLayout(typography_controls)
        typography_form.setContentsMargins(0, 0, 0, 0)
        typography_form.setHorizontalSpacing(14)
        typography_form.setVerticalSpacing(7)
        self._typography_loading = False
        self.type_family = QtWidgets.QComboBox()
        self.type_family.addItem('Inter / UI', 'ui')
        self.type_family.addItem('Iosevka / numeric', 'numeric')
        self.type_numeric_width = QtWidgets.QComboBox()
        self.type_numeric_width.addItem('Normal · dense', 'normal')
        self.type_numeric_width.addItem('Extended · display', 'extended')
        self.type_size_mode = QtWidgets.QComboBox()
        self.type_size_mode.addItem('Point · DPI scaled', 'point')
        self.type_size_mode.addItem('Pixel · A/B test', 'pixel')
        self.type_size = QtWidgets.QDoubleSpinBox()
        self.type_size.setRange(5.0, 24.0)
        self.type_size.setDecimals(2)
        self.type_size.setSingleStep(0.25)
        self.type_weight = QtWidgets.QComboBox()
        for label, value in (
            ('Regular · 400', 400),
            ('Medium · 500', 500),
            ('SemiBold · 600', 600),
            ('Bold · 700', 700),
        ):
            self.type_weight.addItem(label, value)
        self.type_hinting = QtWidgets.QComboBox()
        for label, value in (('Default', 'default'), ('No hinting', 'none'), ('Vertical hinting', 'vertical'), ('Full hinting', 'full')):
            self.type_hinting.addItem(label, value)
        self.type_antialias = QtWidgets.QComboBox()
        for label, value in (('Qt default', 'default'), ('Prefer antialiasing', 'prefer'), ('Disable antialiasing', 'none')):
            self.type_antialias.addItem(label, value)
        self.type_quality = QtWidgets.QComboBox()
        for label, value in (('Qt default', 'default'), ('Prefer quality', 'quality'), ('Prefer font match', 'match')):
            self.type_quality.addItem(label, value)
        self.type_no_subpixel = QtWidgets.QCheckBox('Disable subpixel antialiasing')
        self.type_letter_spacing = QtWidgets.QDoubleSpinBox()
        self.type_letter_spacing.setRange(75.0, 140.0)
        self.type_letter_spacing.setSuffix(' %')
        self.type_letter_spacing.setSingleStep(0.5)
        self.type_word_spacing = QtWidgets.QDoubleSpinBox()
        self.type_word_spacing.setRange(-8.0, 20.0)
        self.type_word_spacing.setSuffix(' px')
        self.type_word_spacing.setSingleStep(0.25)
        self.type_stretch = QtWidgets.QSpinBox()
        self.type_stretch.setRange(50, 200)
        self.type_stretch.setSuffix(' %')
        self.type_kerning = QtWidgets.QCheckBox('Enable kerning')
        self.type_fixed_pitch = QtWidgets.QCheckBox('Declare fixed pitch')
        self.type_painter_aa = QtWidgets.QCheckBox('Custom QPainter TextAntialiasing')
        self.type_dpi_rounding = QtWidgets.QComboBox()
        for label, value in (('Auto · Round Windows / PassThrough elsewhere', 'auto'), ('Round', 'round'), ('PassThrough', 'passthrough'), ('RoundPreferFloor', 'round_prefer_floor'), ('Floor', 'floor'), ('Ceil', 'ceil')):
            self.type_dpi_rounding.addItem(label, value)
        self.type_dpi_rounding.setToolTip('Restart required · selected before QApplication is created')
        self.type_preview = QtWidgets.QLabel('Aa  OI  FUNDING  1234.5678  BTCUSDT.P')
        self.type_preview.setMinimumHeight(34)
        self.type_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.type_preview.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        for hidden_control in (self.type_hinting, self.type_antialias, self.type_quality, self.type_no_subpixel, self.type_letter_spacing, self.type_word_spacing, self.type_stretch, self.type_kerning, self.type_fixed_pitch, self.type_painter_aa, self.type_dpi_rounding):
            hidden_control.setParent(typography_controls)
            hidden_control.hide()
        self.type_rendering_preset = QtWidgets.QComboBox()
        self.type_rendering_preset.addItem('Custom', 'Custom')
        self.type_rendering_preset.addItem('Native', 'Native')
        self.type_rendering_preset.addItem('Balanced', 'Balanced')
        self.type_rendering_preset.addItem('Crisp', 'Crisp')
        typography_form.addRow('Family', self.type_family)
        typography_form.addRow('Numeric width', self.type_numeric_width)
        typography_form.addRow('Size mode', self.type_size_mode)
        typography_form.addRow('Size', self.type_size)
        typography_form.addRow('Weight', self.type_weight)
        typography_form.addRow('Rendering', self.type_rendering_preset)
        typography_form.addRow('Preview', self.type_preview)
        typography_buttons = QtWidgets.QHBoxLayout()
        reset_role = QtWidgets.QPushButton('RESET ROLE')
        reset_role.clicked.connect(self._reset_typography_role)
        reset_type = QtWidgets.QPushButton('RESET TYPOGRAPHY')
        reset_type.clicked.connect(self._reset_typography)
        typography_buttons.addWidget(reset_role)
        typography_buttons.addWidget(reset_type)
        typography_buttons.addStretch(1)
        typography_form.addRow(typography_buttons)
        typography_root.addWidget(typography_controls, 1)
        tabs.addTab(typography_page, 'TYPOGRAPHY')
        for control, key in (
            (self.type_family, 'family'),
            (self.type_numeric_width, 'numeric_width'),
            (self.type_size_mode, 'size_mode'),
            (self.type_weight, 'weight'),
            (self.type_hinting, 'hinting'),
            (self.type_antialias, 'antialias'),
            (self.type_quality, 'quality'),
        ):
            control.currentIndexChanged.connect(lambda _index, name=key, widget=control: self._typography_value_changed(name, widget.currentData()))
        self.type_size.valueChanged.connect(lambda value: self._typography_value_changed('size', float(value)))
        self.type_letter_spacing.valueChanged.connect(lambda value: self._typography_value_changed('letter_spacing', float(value)))
        self.type_word_spacing.valueChanged.connect(lambda value: self._typography_value_changed('word_spacing', float(value)))
        self.type_stretch.valueChanged.connect(lambda value: self._typography_value_changed('stretch', int(value)))
        self.type_kerning.toggled.connect(lambda value: self._typography_value_changed('kerning', bool(value)))
        self.type_fixed_pitch.toggled.connect(lambda value: self._typography_value_changed('fixed_pitch', bool(value)))
        self.type_no_subpixel.toggled.connect(lambda value: self._typography_value_changed('no_subpixel', bool(value)))
        self.type_painter_aa.toggled.connect(lambda value: self._typography_global_changed('painter_text_antialias', bool(value)))
        self.type_dpi_rounding.currentIndexChanged.connect(lambda _index: self._typography_global_changed('dpi_rounding', self.type_dpi_rounding.currentData()))
        self.type_rendering_preset.currentIndexChanged.connect(lambda _index: self._typography_rendering_preset_changed())
        self.typography_roles.currentRowChanged.connect(lambda _row: self._load_typography_role())
        self.typography_roles.setCurrentRow(0)
        self._load_typography_role()
        actions = QtWidgets.QHBoxLayout()
        copy_geometry = QtWidgets.QPushButton('COPY CHART GEOMETRY')
        copy_geometry.setToolTip('Copy price-plot/ViewBox scene geometry for diagnosing dead strips')
        copy_geometry.clicked.connect(self._copy_chart_geometry)
        reset_colors = QtWidgets.QPushButton('THEME COLORS')
        reset_colors.setToolTip('Return shell colors to the active theme')
        reset_colors.clicked.connect(self._reset_colors)
        import_profile = QtWidgets.QPushButton('IMPORT PROFILE')
        import_profile.setToolTip('Import a Nightwatch UI tuner JSON profile and apply it immediately')
        import_profile.clicked.connect(self._import_profile)
        export_profile = QtWidgets.QPushButton('EXPORT PROFILE')
        export_profile.setToolTip('Export the complete effective UI tuner profile as JSON so it can later be promoted to application defaults')
        export_profile.clicked.connect(self._export_profile)
        reset_surfaces = QtWidgets.QPushButton('RESET SURFACES')
        reset_surfaces.setEnabled(False)
        reset_surfaces.setToolTip('Surface geometry is locked by the current build-time contract.')
        reset_geometry = QtWidgets.QPushButton('RESET GEOMETRY')
        reset_geometry.setEnabled(False)
        reset_geometry.setToolTip('Layout geometry is locked by the current build-time contract.')
        actions.addWidget(copy_geometry)
        actions.addWidget(import_profile)
        actions.addWidget(export_profile)
        actions.addStretch(1)
        actions.addWidget(reset_colors)
        actions.addWidget(reset_surfaces)
        actions.addWidget(reset_geometry)
        root.addLayout(actions)

    def _selected_typography_role(self) -> str:
        item = self.typography_roles.currentItem()
        role = str(item.data(Qt.ItemDataRole.UserRole) or '') if item is not None else ''
        return role if role in TYPOGRAPHY_DEFAULTS else TextRole.UI_BODY

    def _set_combo_data(self, combo: QtWidgets.QComboBox, value: object) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(max(0, index))

    def _load_typography_role(self) -> None:
        if not hasattr(self, 'typography_roles'):
            return
        role = self._selected_typography_role()
        profile = typography_controller().profile(role)
        globals_ = typography_controller().globals()
        self._typography_loading = True
        try:
            self._set_combo_data(self.type_family, profile['family'])
            self._set_combo_data(self.type_numeric_width, profile['numeric_width'])
            self.type_numeric_width.setEnabled(profile['family'] == 'numeric')
            self._set_combo_data(self.type_size_mode, profile['size_mode'])
            self.type_size.setValue(float(profile['size']))
            self._set_combo_data(self.type_weight, int(profile['weight']))
            self._set_combo_data(self.type_hinting, profile['hinting'])
            self._set_combo_data(self.type_antialias, profile['antialias'])
            self._set_combo_data(self.type_quality, profile['quality'])
            self.type_no_subpixel.setChecked(bool(profile['no_subpixel']))
            self.type_letter_spacing.setValue(float(profile['letter_spacing']))
            self.type_word_spacing.setValue(float(profile['word_spacing']))
            self.type_stretch.setValue(int(profile['stretch']))
            self.type_kerning.setChecked(bool(profile['kerning']))
            self.type_fixed_pitch.setChecked(bool(profile['fixed_pitch']))
            self.type_painter_aa.setChecked(bool(globals_.get('painter_text_antialias', True)))
            self._set_combo_data(self.type_dpi_rounding, self.host.settings.value('developer/typography_dpi_rounding_v1', 'auto', str))
            self._set_combo_data(self.type_rendering_preset, self._current_typography_rendering_preset(profile))
            self.type_preview.setFont(typography_font(role))
        finally:
            self._typography_loading = False

    @staticmethod
    def _typography_rendering_presets() -> dict[str, dict[str, object]]:
        return {'Native': {'hinting': 'default', 'antialias': 'default', 'quality': 'default', 'no_subpixel': False, 'kerning': True}, 'Balanced': {'hinting': 'vertical', 'antialias': 'prefer', 'quality': 'quality', 'no_subpixel': False, 'kerning': True}, 'Crisp': {'hinting': 'full', 'antialias': 'prefer', 'quality': 'quality', 'no_subpixel': False, 'kerning': True}}

    def _current_typography_rendering_preset(self, profile: dict[str, object]) -> str:
        for name, values in self._typography_rendering_presets().items():
            if all((profile.get(key) == value for key, value in values.items())):
                return name
        return 'Custom'

    def _typography_rendering_preset_changed(self) -> None:
        if self._typography_loading:
            return
        name = str(self.type_rendering_preset.currentData() or '')
        values = self._typography_rendering_presets().get(name)
        if values is None:
            return
        role = self._selected_typography_role()
        self.host.set_developer_typography_values(role, values)
        self._load_typography_role()

    def _typography_value_changed(self, key: str, value: object) -> None:
        if self._typography_loading:
            return
        self.host.set_developer_typography_value(self._selected_typography_role(), key, value)
        if key == 'family':
            self.type_numeric_width.setEnabled(value == 'numeric')
        self.type_preview.setFont(typography_font(self._selected_typography_role()))

    def _typography_global_changed(self, key: str, value: object) -> None:
        if self._typography_loading:
            return
        self.host.set_developer_typography_global(key, value)

    def _reset_typography_role(self) -> None:
        self.host.reset_developer_typography_role(self._selected_typography_role())
        self._load_typography_role()

    def _reset_typography(self) -> None:
        self.host.reset_developer_typography()
        self._load_typography_role()

    def _color_preset_changed(self) -> None:
        name = str(self.color_preset.currentData() or '')
        if name == 'Theme default':
            self.host.apply_developer_ui_color_preset(name)
            self.sync_from_owner()


    def _status_density_changed(self) -> None:
        name = str(self.status_density.currentData() or '')
        if name in DEV_UI_STATUS_PRESETS:
            self.host.apply_developer_ui_status_preset(name)
            self.sync_from_owner()

    def _sync_layout_controls(self) -> None:
        if hasattr(self, 'color_preset'):
            blocker = QtCore.QSignalBlocker(self.color_preset)
            self._set_combo_data(self.color_preset, self.host.current_developer_ui_color_preset())
            del blocker
        if hasattr(self, 'layout_preset'):
            blocker = QtCore.QSignalBlocker(self.layout_preset)
            self._set_combo_data(self.layout_preset, self.host.current_developer_ui_layout_preset())
            del blocker
        if hasattr(self, 'shared_control_spacing'):
            blocker = QtCore.QSignalBlocker(self.shared_control_spacing)
            values = self.host.developer_ui_layout
            spacing = max(int(values.get('toolbar_gap', 0)), int(values.get('timeframe_gap', 0)), int(values.get('instrument_spacing', 0)))
            self.shared_control_spacing.setValue(spacing)
            del blocker
        if hasattr(self, 'status_density'):
            blocker = QtCore.QSignalBlocker(self.status_density)
            self._set_combo_data(self.status_density, self.host.current_developer_ui_status_preset())
            del blocker


    def _add_geometry_group(self, parent: QtWidgets.QVBoxLayout, title: str, fields: tuple) -> None:
        group = QtWidgets.QGroupBox(title)
        grid = QtWidgets.QGridLayout(group)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(0, 1)
        for row, (key, label, minimum, maximum) in enumerate(fields):
            grid.addWidget(QtWidgets.QLabel(label), row, 0)
            spin = QtWidgets.QSpinBox()
            spin.setRange(int(minimum), int(maximum))
            spin.setValue(int(self.host.developer_ui_layout.get(key, DEV_UI_LAYOUT_DEFAULTS[key])))
            spin.setEnabled(False)
            spin.setToolTip('Locked by the current build-time layout contract.')
            grid.addWidget(spin, row, 1)
            self._layout_spins[key] = spin
        parent.addWidget(group)

    def _commit_color(self, key: str, value: str) -> None:
        color = QtGui.QColor(str(value).strip())
        if not color.isValid():
            self._color_edits[key].setText(self.host.ui_theme.get(key, '#000000'))
            return
        normalized = color.name(QtGui.QColor.NameFormat.HexRgb).upper()
        self.host.set_developer_ui_color(key, normalized)
        self._color_edits[key].setText(normalized)
        self._update_color_button(key)

    def _pick_color(self, key: str) -> None:
        initial = QtGui.QColor(self.host.ui_theme.get(key, '#000000'))
        color = QtWidgets.QColorDialog.getColor(initial, self, f'Choose {key} color')
        if not color.isValid():
            return
        value = color.name(QtGui.QColor.NameFormat.HexRgb).upper()
        self.host.set_developer_ui_color(key, value)
        self._color_edits[key].setText(value)
        self._update_color_button(key)

    def _update_color_button(self, key: str) -> None:
        value = self.host.ui_theme.get(key, '#000000')
        color = QtGui.QColor(value)
        foreground = '#000000' if color.lightness() > 145 else '#FFFFFF'
        self._color_buttons[key].setStyleSheet(f'background:{value}; color:{foreground}; border:1px solid #666;')

    def _commit_status_color(self, key: str, value: str) -> None:
        color = QtGui.QColor(str(value).strip())
        if not color.isValid():
            self._status_color_edits[key].setText(str(self.host.developer_ui_status[key]))
            return
        normalized = color.name(QtGui.QColor.NameFormat.HexRgb).upper()
        self.host.set_developer_ui_status_value(key, normalized)
        self._status_color_edits[key].setText(normalized)
        self._update_status_color_button(key)

    def _pick_status_color(self, key: str) -> None:
        initial = QtGui.QColor(str(self.host.developer_ui_status[key]))
        color = QtWidgets.QColorDialog.getColor(initial, self, f'Choose status {key} color')
        if color.isValid():
            self._commit_status_color(key, color.name(QtGui.QColor.NameFormat.HexRgb))

    def _update_status_color_button(self, key: str) -> None:
        value = str(self.host.developer_ui_status[key])
        color = QtGui.QColor(value)
        foreground = '#000000' if color.lightness() > 145 else '#FFFFFF'
        self._status_color_buttons[key].setStyleSheet(f'background:{value}; color:{foreground}; border:1px solid #666;')

    def sync_from_owner(self) -> None:
        for key, edit in self._color_edits.items():
            edit.setText(self.host.ui_theme.get(key, '#000000'))
            self._update_color_button(key)
        for key, spin in self._layout_spins.items():
            blocker = QtCore.QSignalBlocker(spin)
            spin.setValue(int(self.host.developer_ui_layout.get(key, DEV_UI_LAYOUT_DEFAULTS[key])))
            del blocker
        for key, spin in getattr(self, '_surface_spins', {}).items():
            blocker = QtCore.QSignalBlocker(spin)
            spin.setValue(int(self.host.developer_ui_surfaces.get(key, DEV_UI_SURFACE_DEFAULTS[key])))
            del blocker
        for key, edit in getattr(self, '_status_color_edits', {}).items():
            edit.setText(str(self.host.developer_ui_status[key]))
            self._update_status_color_button(key)
        for key, combo in getattr(self, '_status_font_combos', {}).items():
            blocker = QtCore.QSignalBlocker(combo)
            self._set_combo_data(combo, self.host.developer_ui_status[key])
            del blocker
        self._sync_layout_controls()
        if hasattr(self, 'typography_roles'):
            self._load_typography_role()

    def _reset_colors(self) -> None:
        self.host.reset_developer_ui_colors()
        self.sync_from_owner()


    def _reset_status(self) -> None:
        self.host.reset_developer_ui_status()
        self.sync_from_owner()

    def _import_profile(self) -> None:
        self.host.import_developer_ui_profile()

    def _export_profile(self) -> None:
        self.host.export_developer_ui_profile()

    def _copy_chart_geometry(self) -> None:
        report = self.host.chart.developer_geometry_report()
        QtWidgets.QApplication.clipboard().setText(report)
        self.host.statusBar().showMessage('CHART GEOMETRY COPIED', 1800)
from ..models import MagneticRailLabHostPort
from ..chart.magnetic_rail import ORDER_RAIL_LAB_DEFAULTS, ORDER_RAIL_STYLE_PRESETS, ORDER_RAIL_ORDER_PRESET_DEFAULTS, normalized_order_rail_order_preset
from ..constants import LEVERAGE_PRESETS

class MagneticRailLabDialog(QtWidgets.QWidget):
    """Magnetic rail visual preset studio."""
    _RAY_NOTES = {'Flux Arc / Photon Sweep': 'Flux Arc capture with one clean photon sweep across the remaining line.', 'Flux Arc / Pulse Lance': 'Flux Arc capture with a faster concentrated lance pulse.', 'Flux Arc / Vector Stream': 'Flux Arc capture with continuous moving vectors and chevrons across the full left rail.', 'Flux Arc / Reactor Wave': 'Flux Arc capture with twin reactor rails and a breathing core.', 'Flux Arc / Prism Packets': 'Flux Arc capture with three restrained semantic-color packets.', 'Flux Arc / Scanline': 'Flux Arc capture with a narrow scanning highlight window.', 'Flux Arc / Quantum Dash': 'Flux Arc capture with a phase-shifting dash train.', 'Flux Arc / Comet Trail': 'Flux Arc capture with layered comet trails moving toward the arc.', 'Flux Arc / Interference': 'Flux Arc capture with traveling interference bands along the line.', 'Flux Arc / Charge Relay': 'Flux Arc capture with sequential field nodes relaying charge toward it.'}

    def __init__(self, host: MagneticRailLabHostPort):
        super().__init__(host)
        self.host = host
        self._syncing = False
        self.setObjectName('settingsEmbeddedTool')
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)
        heading = QtWidgets.QLabel('MAGNETIC RAIL DESIGN')
        heading.setObjectName('dialogHeading')
        root.addWidget(heading)
        note = QtWidgets.QLabel("The rail stays pinned to the chart's right edge. Order size, leverage and reduce-only are now compact inline controls on the rail body; the old expandable tooltip/tray frontend has been removed.")
        note.setObjectName('subtleLabel')
        note.setWordWrap(True)
        root.addWidget(note)
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.style_combo = QtWidgets.QComboBox()
        self.style_combo.addItems(tuple(ORDER_RAIL_STYLE_PRESETS))
        self.style_combo.currentTextChanged.connect(self._style_selected)
        form.addRow('RAY EFFECT', self.style_combo)
        self.armed_transition_check = QtWidgets.QCheckBox('Animate contraction + directional latch')
        self.armed_transition_check.setToolTip('Animate the armed rail shrinking from the left while its right-axis price alignment stays fixed. Off keeps the same compact armed end-state but snaps to it immediately.')
        self.armed_transition_check.toggled.connect(self._armed_transition_toggled)
        form.addRow('ARM TRANSITION', self.armed_transition_check)
        self.cancel_implosion_check = QtWidgets.QCheckBox('Animate cancellation implosion')
        self.cancel_implosion_check.setToolTip("When a parked armed rail's working order disappears, collapse it into the right-side capture gate before removal. Off removes it without the implosion transition.")
        self.cancel_implosion_check.toggled.connect(self._cancel_implosion_toggled)
        form.addRow('CANCEL EFFECT', self.cancel_implosion_check)
        root.addLayout(form)
        self.preview_note = QtWidgets.QLabel()
        self.preview_note.setObjectName('subtleLabel')
        self.preview_note.setWordWrap(True)
        self.preview_note.setMinimumHeight(72)
        root.addWidget(self.preview_note)
        root.addStretch(1)
        bottom = QtWidgets.QHBoxLayout()
        reset = QtWidgets.QPushButton('RESTORE RAIL DEFAULTS')
        reset.clicked.connect(self._reset)
        bottom.addWidget(reset)
        bottom.addStretch(1)
        root.addLayout(bottom)
        self.sync_from_owner()

    def _apply_choice(self, key: str, value: Any) -> None:
        if self._syncing:
            return
        config = dict(self.host.magnetic_rail_config)
        config[key] = value
        self.host.set_magnetic_rail_config(config)
        self.sync_from_owner()

    def _style_selected(self, style: str) -> None:
        self._apply_choice('style', style)

    def _armed_transition_toggled(self, enabled: bool) -> None:
        self._apply_choice('armed_transition_enabled', bool(enabled))

    def _cancel_implosion_toggled(self, enabled: bool) -> None:
        self._apply_choice('cancel_implosion_enabled', bool(enabled))

    def _refresh_note(self) -> None:
        style = self.style_combo.currentText()
        arm_state = 'ON' if self.armed_transition_check.isChecked() else 'OFF · SNAP'
        cancel_state = 'ON' if self.cancel_implosion_check.isChecked() else 'OFF · IMMEDIATE REMOVE'
        self.preview_note.setText(f'RAY · {self._RAY_NOTES.get(style, style)}\nINLINE CONTROLS · SIZE CYCLE · LEVERAGE CYCLE · REDUCE-ONLY TOGGLE\nARM TRANSITION · {arm_state}\nCANCEL IMPLOSION · {cancel_state}')

    def sync_from_owner(self) -> None:
        self._syncing = True
        try:
            cfg = self.host.magnetic_rail_config
            self.style_combo.setCurrentText(str(cfg.get('style', 'Flux Arc / Photon Sweep')))
            self.armed_transition_check.setChecked(bool(cfg.get('armed_transition_enabled', True)))
            self.cancel_implosion_check.setChecked(bool(cfg.get('cancel_implosion_enabled', True)))
        finally:
            self._syncing = False
        self._refresh_note()

    def _reset(self) -> None:
        self.host.set_magnetic_rail_config(dict(ORDER_RAIL_LAB_DEFAULTS))
        self.sync_from_owner()

class MagneticRailPresetsDialog(QtWidgets.QDialog):
    """Trading-menu presets used by Ctrl+left/right chart rail placement."""
    ORDER_TYPES = (('LIMIT ENTRY', 'LIMIT'), ('STOP LIMIT', 'STOP'), ('STOP MARKET', 'STOP_MARKET'), ('TAKE PROFIT LIMIT', 'TAKE_PROFIT'), ('TAKE PROFIT MARKET', 'TAKE_PROFIT_MARKET'), ('TRAILING STOP', 'TRAILING_STOP_MARKET'))

    def __init__(self, presets: dict[str, dict[str, Any]], active_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Magnetic rail trading presets')
        self.resize(760, 570)
        self.setMinimumSize(690, 520)
        self._loading = False
        self._presets = {str(k): normalized_order_rail_order_preset(v) for k, v in presets.items()} or {k: normalized_order_rail_order_preset(v) for k, v in ORDER_RAIL_ORDER_PRESET_DEFAULTS.items()}
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)
        note = QtWidgets.QLabel('Ctrl + left-click chart = BUY/LONG rail · Ctrl + right-click chart = SELL/SHORT rail. The rail body exposes inline size, leverage and reduce-only controls; the saved preset supplies the remaining execution defaults.')
        note.setObjectName('subtleLabel')
        note.setWordWrap(True)
        root.addWidget(note)
        ar = QtWidgets.QHBoxLayout()
        ar.addWidget(QtWidgets.QLabel('CTRL-CLICK PRESET'))
        self.active = QtWidgets.QComboBox()
        ar.addWidget(self.active, 1)
        root.addLayout(ar)
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)
        self.names = QtWidgets.QListWidget()
        self.names.setMinimumWidth(190)
        self.names.setMaximumWidth(230)
        body.addWidget(self.names)
        host = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(host)
        form.setContentsMargins(8, 4, 4, 4)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setMaxLength(32)
        form.addRow('Preset name', self.name_edit)
        self.order_type = QtWidgets.QComboBox()
        [self.order_type.addItem(a, b) for a, b in self.ORDER_TYPES]
        form.addRow('Order type', self.order_type)
        self.order_role = QtWidgets.QComboBox()
        [self.order_role.addItem(a, b) for a, b in (('ENTRY', 'ENTRY'), ('TAKE PROFIT', 'TP'), ('STOP LOSS', 'SL'))]
        form.addRow('Role', self.order_role)
        self.reduce_only = QtWidgets.QCheckBox('Reduce position only')
        form.addRow('Reduce only', self.reduce_only)
        self.size_percent = QtWidgets.QComboBox()
        [self.size_percent.addItem(f'{value}%', value) for value in (25, 50, 75, 100)]
        form.addRow('Default allocation', self.size_percent)
        self.leverage = QtWidgets.QComboBox()
        [self.leverage.addItem(f'{value}×', value) for value in LEVERAGE_PRESETS]
        form.addRow('Default leverage', self.leverage)
        self.time_in_force = QtWidgets.QComboBox()
        self.time_in_force.addItem('GTC', 'GTC')
        self.time_in_force.addItem('GTX · POST-ONLY', 'GTX')
        self.time_in_force.addItem('IOC', 'IOC')
        self.time_in_force.addItem('FOK', 'FOK')
        form.addRow('Time in force', self.time_in_force)
        self.working_type = QtWidgets.QComboBox()
        self.working_type.addItem('Last / contract price', 'CONTRACT_PRICE')
        self.working_type.addItem('Mark price', 'MARK_PRICE')
        form.addRow('Trigger source', self.working_type)
        self.price_protect = QtWidgets.QCheckBox('Enable Binance price protection')
        form.addRow('Price protect', self.price_protect)
        self.callback_rate = QtWidgets.QDoubleSpinBox()
        self.callback_rate.setRange(0.1, 10.0)
        self.callback_rate.setSingleStep(0.1)
        self.callback_rate.setDecimals(1)
        self.callback_rate.setSuffix('%')
        form.addRow('Trailing callback', self.callback_rate)
        self.limit_offset = QtWidgets.QDoubleSpinBox()
        self.limit_offset.setRange(-5.0, 5.0)
        self.limit_offset.setSingleStep(0.025)
        self.limit_offset.setDecimals(3)
        self.limit_offset.setSuffix('%')
        self.limit_offset.setToolTip('Signed limit-price offset from the rail trigger for stop-limit / TP-limit presets.')
        form.addRow('Limit offset', self.limit_offset)
        self.line_pattern = QtWidgets.QComboBox()
        self.line_pattern.addItem('Use visual style', 'inherit')
        self.line_pattern.addItem('Solid', 'solid')
        self.line_pattern.addItem('Dashed', 'dash')
        self.line_pattern.addItem('Dotted', 'dot')
        self.line_pattern.addItem('Segmented / trailing', 'segmented')
        form.addRow('Line treatment', self.line_pattern)
        body.addWidget(host, 1)
        root.addLayout(body, 1)
        er = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton('ADD PRESET')
        dup = QtWidgets.QPushButton('DUPLICATE')
        rem = QtWidgets.QPushButton('REMOVE')
        er.addWidget(add)
        er.addWidget(dup)
        er.addWidget(rem)
        er.addStretch(1)
        root.addLayout(er)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel | QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults).setText('RESTORE DEFAULTS')
        root.addWidget(buttons)
        self.names.currentRowChanged.connect(self._load_selected)
        self.name_edit.editingFinished.connect(self._rename_selected)
        for signal in (self.order_type.currentIndexChanged, self.order_role.currentIndexChanged, self.reduce_only.toggled, self.size_percent.currentIndexChanged, self.leverage.currentIndexChanged, self.time_in_force.currentTextChanged, self.working_type.currentIndexChanged, self.price_protect.toggled, self.callback_rate.valueChanged, self.limit_offset.valueChanged, self.line_pattern.currentIndexChanged):
            signal.connect(self._field_changed)
        add.clicked.connect(self._add)
        dup.clicked.connect(self._duplicate)
        rem.clicked.connect(self._remove)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._restore_defaults)
        self._refresh(active_name)

    def _refresh(self, selected=''):
        if not self._presets:
            self._presets = {k: normalized_order_rail_order_preset(v) for k, v in ORDER_RAIL_ORDER_PRESET_DEFAULTS.items()}
        selected = selected if selected in self._presets else next(iter(self._presets))
        self._loading = True
        self.names.clear()
        self.active.clear()
        for n in self._presets:
            self.names.addItem(n)
            self.active.addItem(n)
        i = list(self._presets).index(selected)
        self.names.setCurrentRow(i)
        self.active.setCurrentIndex(i)
        self._loading = False
        self._load_selected(i)

    def _name(self):
        item = self.names.currentItem()
        return item.text() if item else ''

    def _load_selected(self, row):
        if row < 0 or row >= self.names.count():
            return
        n = self.names.item(row).text()
        d = self._presets[n]
        self._loading = True
        self.name_edit.setText(n)
        self.order_type.setCurrentIndex(max(0, self.order_type.findData(d['orderType'])))
        self.order_role.setCurrentIndex(max(0, self.order_role.findData(d['orderRole'])))
        self.reduce_only.setChecked(bool(d['reduceOnly']))
        self.size_percent.setCurrentIndex(max(0, self.size_percent.findData(int(d['sizePercent']))))
        self.leverage.setCurrentIndex(max(0, self.leverage.findData(int(d.get('leverage', 5)))))
        self.time_in_force.setCurrentIndex(max(0, self.time_in_force.findData(str(d['timeInForce']))))
        self.working_type.setCurrentIndex(max(0, self.working_type.findData(d['workingType'])))
        self.price_protect.setChecked(bool(d['priceProtect']))
        self.callback_rate.setValue(float(d['callbackRate']))
        self.limit_offset.setValue(float(d['limitOffsetPercent']))
        self.line_pattern.setCurrentIndex(max(0, self.line_pattern.findData(d.get('linePattern', 'inherit'))))
        self._loading = False
        self._sync_enabled()

    def _sync_enabled(self):
        typ = str(self.order_type.currentData())
        conditional = typ in {'STOP', 'STOP_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET', 'TRAILING_STOP_MARKET'}
        self.time_in_force.setEnabled(typ in {'LIMIT', 'STOP', 'TAKE_PROFIT'})
        self.working_type.setEnabled(conditional)
        self.price_protect.setEnabled(conditional)
        self.callback_rate.setEnabled(typ == 'TRAILING_STOP_MARKET')
        self.limit_offset.setEnabled(typ in {'STOP', 'TAKE_PROFIT'})

    def _field_changed(self, *_):
        if self._loading:
            return
        n = self._name()
        if not n:
            return
        role = str(self.order_role.currentData())
        reducing = bool(self.reduce_only.isChecked()) or role in {'TP', 'SL'}
        self._presets[n] = normalized_order_rail_order_preset({'orderType': str(self.order_type.currentData()), 'orderRole': role, 'reduceOnly': reducing, 'sizePercent': int(self.size_percent.currentData() or 25), 'leverage': int(self.leverage.currentData() or 5), 'timeInForce': str(self.time_in_force.currentData() or 'GTC'), 'workingType': str(self.working_type.currentData()), 'priceProtect': self.price_protect.isChecked(), 'callbackRate': float(self.callback_rate.value()), 'limitOffsetPercent': float(self.limit_offset.value()), 'linePattern': str(self.line_pattern.currentData())})
        if reducing and (not self.reduce_only.isChecked()):
            self._loading = True
            self.reduce_only.setChecked(True)
            self._loading = False
        self._sync_enabled()

    def _rename_selected(self):
        if self._loading:
            return
        old = self._name()
        new = self.name_edit.text().strip()
        if not old or not new or (new != old and new.casefold() in {x.casefold() for x in self._presets}):
            self.name_edit.setText(old)
            return
        if new == old:
            return
        active = self.active.currentText()
        self._presets = {new if k == old else k: v for k, v in self._presets.items()}
        self._refresh(new)
        self.active.setCurrentText(new if active == old else active)

    def _add(self):
        self._field_changed()
        base = 'New preset'
        n = base
        i = 2
        while n.casefold() in {x.casefold() for x in self._presets}:
            n = f'{base} {i}'
            i += 1
        self._presets[n] = normalized_order_rail_order_preset(ORDER_RAIL_ORDER_PRESET_DEFAULTS['Limit Entry'])
        self._refresh(n)
        self.name_edit.selectAll()
        self.name_edit.setFocus()

    def _duplicate(self):
        src = self._name()
        if not src:
            return
        self._field_changed()
        base = f'{src} copy'
        n = base
        i = 2
        while n.casefold() in {x.casefold() for x in self._presets}:
            n = f'{base} {i}'
            i += 1
        self._presets[n] = dict(self._presets[src])
        self._refresh(n)

    def _remove(self):
        if len(self._presets) <= 1:
            return
        self._presets.pop(self._name(), None)
        self._refresh()

    def _restore_defaults(self):
        self._presets = {k: normalized_order_rail_order_preset(v) for k, v in ORDER_RAIL_ORDER_PRESET_DEFAULTS.items()}
        self._refresh('Limit Entry')

    def _accept(self):
        self._field_changed()
        self._rename_selected()
        self.accept()

    def values(self):
        active = self.active.currentText()
        active = active if active in self._presets else next(iter(self._presets))
        return ({k: normalized_order_rail_order_preset(v) for k, v in self._presets.items()}, active)
import time
from PySide6.QtCore import QTimer
from ..models import DiagnosticsHostPort
from ..constants import TESTING_ENTRIES
from ..networking.binance import BINANCE_RATE_LIMITER

class DeveloperDialog(QtWidgets.QWidget):
    REFRESH_MS = 500

    def __init__(self, host: DiagnosticsHostPort):
        from ..utilities import get_diagnostics
        self._diagnostics = get_diagnostics()
        super().__init__(host)
        self.host = host
        self.setObjectName('settingsEmbeddedTool')
        self._last_counters: dict[str, int] = {}
        self._last_refresh = time.monotonic()
        self._lag_ms = 0.0
        self._peak_lag_ms = 0.0
        self._last_stall_logged = 0.0
        self._last_log_signature: tuple[Any, ...] | None = None
        self._last_chart_metrics: dict[str, int] = {}
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel('DEVELOPER / DIAGNOSTICS')
        title.setObjectName('dialogHeading')
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(QtWidgets.QLabel('Log detail'))
        self.verbosity = QtWidgets.QComboBox()
        self.verbosity.addItems(('Errors only', 'Normal', 'Verbose'))
        saved_verbosity = max(0, min(2, host.settings.value('developer/log_verbosity_v1', 1, int)))
        self.verbosity.setCurrentIndex(saved_verbosity)
        self.verbosity.currentIndexChanged.connect(self._verbosity_changed)
        header.addWidget(self.verbosity)
        root.addLayout(header)
        note = QtWidgets.QLabel('High-frequency activity is summarized as counters/rates. The log stays concise even in Verbose mode.')
        note.setObjectName('subtleLabel')
        note.setWordWrap(True)
        root.addWidget(note)
        summary_box = QtWidgets.QGroupBox('LIVE HEALTH')
        summary_grid = QtWidgets.QGridLayout(summary_box)
        summary_grid.setContentsMargins(10, 8, 10, 8)
        summary_grid.setHorizontalSpacing(18)
        summary_grid.setVerticalSpacing(5)
        self.summary_labels: dict[str, QtWidgets.QLabel] = {}
        summary_rows = (('rest', 'REST'), ('latency', 'REST latency'), ('ws', 'WebSocket traffic'), ('limits', 'Binance limits'), ('render', 'Rendering'), ('dom', 'DOM renderer'), ('orderflow_cost', 'Order-flow analyzer'), ('orderflow_cache', 'Order-flow level cache'), ('microstructure_cost', 'Microstructure analyzer'), ('aggregation_cost', 'DOM aggregation'), ('orderflow_state', 'Order-flow state'), ('depth_pipeline', 'Depth pipeline'), ('chart_path', 'Chart render path'), ('renderer', 'Chart renderer'), ('lag', 'UI timer lag'), ('health', 'Errors / reconnects'))
        for index, (key, label) in enumerate(summary_rows):
            row = index // 2
            column = index % 2 * 2
            name = QtWidgets.QLabel(label)
            name.setObjectName('subtleLabel')
            value = QtWidgets.QLabel('—')
            if key in {'orderflow_cost', 'orderflow_cache', 'microstructure_cost', 'aggregation_cost', 'orderflow_state', 'depth_pipeline', 'chart_path', 'renderer'}:
                value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            summary_grid.addWidget(name, row, column)
            summary_grid.addWidget(value, row, column + 1)
            self.summary_labels[key] = value
        root.addWidget(summary_box)
        frame_box = QtWidgets.QGroupBox('30-SECOND FRAME PROFILE')
        frame_layout = QtWidgets.QHBoxLayout(frame_box)
        frame_layout.setContentsMargins(10, 8, 10, 8)
        self.frame_profile_button = QtWidgets.QPushButton('START 30S CAPTURE')
        self.frame_profile_button.setToolTip(
            'Pan/zoom continuously during capture. Measures the chart receiving input; '
            'Qt swap cadence when available, otherwise chart paints. '
            'Neither source proves physical display scanout. 1% low uses the mean of the slowest 1% of intervals.'
        )
        self.frame_profile_button.clicked.connect(self._start_frame_profile)
        frame_layout.addWidget(self.frame_profile_button)
        self.frame_profile_label = QtWidgets.QLabel('Not captured yet.')
        self.frame_profile_label.setWordWrap(True)
        self.frame_profile_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        frame_layout.addWidget(self.frame_profile_label, 1)
        root.addWidget(frame_box)
        endpoint_box = QtWidgets.QGroupBox('REST ENDPOINTS')
        endpoint_layout = QtWidgets.QVBoxLayout(endpoint_box)
        endpoint_layout.setContentsMargins(10, 7, 10, 7)
        self.endpoint_label = QtWidgets.QLabel('No REST requests recorded yet.')
        self.endpoint_label.setObjectName('subtleLabel')
        self.endpoint_label.setWordWrap(True)
        self.endpoint_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        endpoint_layout.addWidget(self.endpoint_label)
        root.addWidget(endpoint_box)
        testing_box = QtWidgets.QGroupBox('TESTING / RENDER OPTIONS')
        testing_grid = QtWidgets.QGridLayout(testing_box)
        testing_grid.setContentsMargins(10, 8, 10, 8)
        testing_grid.setHorizontalSpacing(18)
        testing_grid.setVerticalSpacing(6)
        self.testing_checks: dict[str, QtWidgets.QCheckBox] = {}
        self.testing_labels: dict[str, str] = {}
        for index, (flag_name, label, tooltip) in enumerate(TESTING_ENTRIES):
            checkbox = QtWidgets.QCheckBox()
            checkbox.setToolTip(tooltip)
            checkbox.setChecked(bool(host.testing_flags.get(flag_name)))
            checkbox.toggled.connect(lambda checked, name=flag_name: host._set_testing_flag(name, checked))
            testing_grid.addWidget(checkbox, index // 2, index % 2)
            self.testing_checks[flag_name] = checkbox
            self.testing_labels[flag_name] = label
        root.addWidget(testing_box)
        log_box = QtWidgets.QGroupBox('EVENTS')
        log_layout = QtWidgets.QVBoxLayout(log_box)
        log_layout.setContentsMargins(8, 8, 8, 8)
        log_controls = QtWidgets.QHBoxLayout()
        log_controls.addStretch(1)
        clear_button = QtWidgets.QPushButton('Clear log')
        clear_button.clicked.connect(self._clear_log)
        log_controls.addWidget(clear_button)
        log_layout.addLayout(log_controls)
        self.log_table = QtWidgets.QTableWidget(0, 4)
        self.log_table.setHorizontalHeaderLabels(('TIME', 'TYPE', 'AREA', 'MESSAGE'))
        self.log_table.verticalHeader().hide()
        self.log_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.log_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.log_table.setAlternatingRowColors(False)
        self.log_table.setSortingEnabled(False)
        header_view = self.log_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        log_layout.addWidget(self.log_table, 1)
        root.addWidget(log_box, 1)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(self.REFRESH_MS)
        self.refresh_timer.timeout.connect(self._refresh)
        self.sync_testing_state()
        self._refresh()

    def _start_frame_profile(self) -> None:
        clock = getattr(self.host, 'presentation_clock', None)
        starter = getattr(clock, 'start_frame_profile', None)
        if callable(starter):
            starter(30.0)
            self._refresh_frame_profile()

    def _refresh_frame_profile(self) -> None:
        clock = getattr(self.host, 'presentation_clock', None)
        getter = getattr(clock, 'frame_profile', None)
        if not callable(getter):
            self.frame_profile_button.setEnabled(False)
            self.frame_profile_label.setText('Presentation clock profiling unavailable.')
            return
        profile = getter()
        if not profile.get('completed') and not profile.get('active'):
            return
        active = bool(profile.get('active'))
        self.frame_profile_button.setEnabled(not active)
        if active:
            self.frame_profile_button.setText(f"CAPTURING · {float(profile.get('remaining_s', 0.0)):.0f}S")
        else:
            self.frame_profile_button.setText('START 30S CAPTURE')
        source_label = {
            'chart_swap': 'Qt swap cadence',
            'chart_paint': 'chart paint cadence',
            'no_render_samples': 'no rendered frame samples',
        }.get(profile.get('sample_source'), 'no rendered frame samples')
        self.frame_profile_label.setText(
            f"{profile.get('sample_surface', 'Chart')}: {source_label} · "
            f"{float(profile.get('avg_fps', 0.0)):.1f} capture avg FPS / {float(profile.get('target_fps', 0.0)):.0f} Hz target · "
            f"frame {float(profile.get('frame_avg_ms', 0.0)):.2f} avg · "
            f"p50 {float(profile.get('frame_p50_ms', 0.0)):.2f} · "
            f"p95 {float(profile.get('frame_p95_ms', 0.0)):.2f} · "
            f"p99 {float(profile.get('frame_p99_ms', 0.0)):.2f} · "
            f"max {float(profile.get('frame_max_ms', 0.0)):.2f} ms · "
            f"1% low {float(profile.get('one_percent_low_fps', 0.0)):.1f} FPS · "
            f"{int(profile.get('frame_count', 0))} frames · "
            f"{int(profile.get('estimated_missed_refresh_slots', 0))} estimated missed display slots"
            + "".join(f"\n{surface['name']}: {surface['paint_rate_fps']:.1f} paints/s · {surface['swap_rate_fps']:.1f} swaps/s · p99 {surface['frame_p99_ms']:.2f} ms · max {surface['frame_max_ms']:.2f} ms"
                      for surface in profile.get('surfaces', []))
        )

    def _verbosity_changed(self, index: int) -> None:
        self.host.settings.setValue('developer/log_verbosity_v1', int(index))
        self._refresh_log(self._diagnostics.snapshot()['events'])

    def _clear_log(self) -> None:
        self._diagnostics.clear_events()
        self._last_log_signature = None
        self.log_table.setRowCount(0)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        snapshot = self._diagnostics.snapshot()
        self._last_counters = dict(snapshot['counters'])
        state_getter = getattr(self.host.chart, 'diagnostic_state', None)
        if callable(state_getter):
            render_path = state_getter().get('render_path', {})
            self._last_chart_metrics = {key: int(render_path.get(key, 0)) for key in ('native_draws', 'native_failures', 'fallback_frames', 'cpu_paints')}
        else:
            self._last_chart_metrics = {}
        self._last_refresh = time.monotonic()
        if not self.refresh_timer.isActive():
            self.refresh_timer.start()
        self._refresh()

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self.refresh_timer.stop()
        super().hideEvent(event)

    def sync_testing_state(self) -> None:
        for name, checkbox in self.testing_checks.items():
            enabled = bool(self.host.testing_flags.get(name))
            blocker = QtCore.QSignalBlocker(checkbox)
            checkbox.setChecked(enabled)
            locked = name == 'chart_opengl_full_viewport' and bool(self.host.testing_flags.get('chart_opengl'))
            checkbox.setEnabled(not locked)
            suffix = 'ON' if enabled else 'OFF'
            if locked:
                suffix += ' · LOCKED'
            checkbox.setText(f'{self.testing_labels.get(name, name)}  ·  {suffix}')
            del blocker

    @staticmethod
    def _timing_text(values: tuple[float, ...]) -> str:
        if not values:
            return '—'
        average = sum(values) / len(values)
        return f'{average:.0f} ms avg · {max(values):.0f} ms max'

    @staticmethod
    def _timing_profile(values: tuple[float, ...], *, precision: int=2) -> str:
        if not values:
            return '—'
        ordered = sorted((float(value) for value in values))

        def percentile(fraction: float) -> float:
            index = int(round((len(ordered) - 1) * fraction))
            return ordered[max(0, min(len(ordered) - 1, index))]
        return f'p50 {percentile(0.5):.{precision}f} · p95 {percentile(0.95):.{precision}f} · max {ordered[-1]:.{precision}f} ms'

    @staticmethod
    def _chart_gpu_detail(render_path: dict[str, Any], native_rate: float) -> str:
        batches = render_path.get('gpu_batches', {})
        if not isinstance(batches, dict):
            batches = {}

        requested = render_path.get('requested_opengl')
        if requested is None:
            requested = render_path.get('native_renderer_requested')
        if requested is None and batches:
            requested = any(bool(batch.get('requested_native')) for batch in batches.values() if isinstance(batch, dict))
        request_text = 'viewport GL requested' if requested is True else 'viewport software selected' if requested is False else 'viewport request unknown'
        native_requested = render_path.get('requested_native')
        if native_requested is not None:
            request_text += ' · native bars requested' if native_requested else ' · native bars disabled'

        actual = str(render_path.get('gpu_status', render_path.get('actual_render_path', 'unverified')) or 'unverified')
        viewport = render_path.get('actual_viewport', '')
        if isinstance(viewport, bool):
            viewport = 'OpenGL viewport' if viewport else 'raster viewport'
        viewport_text = f' · {viewport}' if viewport else ''
        native_draws = int(render_path.get('native_draws', sum(int(batch.get('native_draws', 0)) for batch in batches.values() if isinstance(batch, dict))))
        failures = int(render_path.get('native_failures', sum(int(batch.get('native_failures', 0)) for batch in batches.values() if isinstance(batch, dict))))
        fallback_frames = int(render_path.get('fallback_frames', sum(int(batch.get('fallback_frames', 0)) for batch in batches.values() if isinstance(batch, dict))))
        cpu_paints = int(render_path.get('cpu_paints', sum(int(batch.get('cpu_paints', 0)) for batch in batches.values() if isinstance(batch, dict))))
        detail = f'{request_text}{viewport_text} · actual {actual} · native {native_rate:.0f}/s ({native_draws} total) · fallback {fallback_frames} frames / {failures} failures · CPU {cpu_paints} paints'

        context = render_path.get('last_context', {})
        if not isinstance(context, dict) or not context:
            context = next(
                (batch.get('last_context', {}) for batch in batches.values()
                 if isinstance(batch, dict) and batch.get('last_context')),
                {},
            )
        if isinstance(context, dict):
            hardware = ' · '.join(str(context.get(key, '')).strip() for key in ('vendor', 'renderer', 'version') if context.get(key))
            if hardware:
                detail += f" · {'last context ' if context.get('current') is False else ''}{hardware}"
            context_format = str(context.get('context_format', '') or '')
            if context_format:
                detail += f' · {context_format}'
        requested_format = str(render_path.get('requested_context_format', '') or '')
        if requested_format and requested_format != 'disabled':
            detail += f' · requested {requested_format}'

        batch_details = []
        for name, batch in batches.items():
            if not isinstance(batch, dict):
                continue
            profile = str(batch.get('profile', name))
            batch_details.append(
                f"{profile} {batch.get('observed_path', 'unverified')} "
                f"N{int(batch.get('native_draws', 0))}/F{int(batch.get('fallback_frames', 0))}"
            )
        if batch_details:
            detail += ' · ' + ', '.join(batch_details[:4])

        reasons = render_path.get('fallback_reasons', {})
        if isinstance(reasons, dict):
            active_reasons = [f'{reason} {int(count)}' for reason, count in reasons.items() if int(count) > 0]
            if active_reasons:
                detail += ' · reasons ' + ', '.join(active_reasons[:2])
        failure = str(render_path.get('last_native_failure', '') or '')
        if failure and failure not in detail and actual == 'fallback':
            detail += f' · {failure}'
        runtime_failure = str(render_path.get('runtime_fallback_reason', '') or '')
        if runtime_failure and runtime_failure not in detail:
            detail += f' · viewport: {runtime_failure}'
        return detail

    def _refresh(self) -> None:
        self._refresh_frame_profile()
        now = time.monotonic()
        elapsed = max(0.001, now - self._last_refresh)
        self._lag_ms = max(0.0, (elapsed - self.REFRESH_MS / 1000.0) * 1000.0)
        self._peak_lag_ms = max(self._peak_lag_ms, self._lag_ms)
        if self._lag_ms >= 250.0 and now - self._last_stall_logged >= 2.0:
            self._last_stall_logged = now
            self._diagnostics.increment('ui.stalls')
            self._diagnostics.warning('UI', f'event-loop delay · {self._lag_ms:.0f} ms')
        snapshot = self._diagnostics.snapshot()
        counters = snapshot['counters']
        timings = snapshot['timings']

        def total(key: str) -> int:
            return int(counters.get(key, 0))

        def rate(key: str) -> float:
            current = total(key)
            previous = int(self._last_counters.get(key, current))
            return max(0.0, (current - previous) / elapsed)
        rest_rate = rate('rest.requests')
        trade_rate = rate('trade_ws.requests')
        ws_rates = {kind: rate(f'ws.in.{kind}') for kind in ('public', 'market', 'ticker')}
        chart_rate = rate('render.chart_paints')
        book_rate = rate('render.orderbook_depth_paints') + rate('render.orderbook_ladder_paints')
        book_updates = rate('orderbook.updates')
        self.summary_labels['rest'].setText(f"{rest_rate:.1f}/s · {total('rest.requests')} total · trade WS {trade_rate:.1f}/s")
        self.summary_labels['latency'].setText(self._timing_text(timings.get('rest.latency_ms', ())))
        self.summary_labels['ws'].setText(' · '.join((f'{kind} {ws_rates[kind]:.0f}/s' for kind in ('public', 'market', 'ticker'))) + f" · account {rate('user_ws.messages'):.1f}/s")
        limiter = BINANCE_RATE_LIMITER.diagnostic_snapshot()
        rest_used, rest_limit = limiter['rest']
        ws_used, ws_limit = limiter['ws']
        order_used, order_limit = limiter['orders']
        blocked = float(limiter['blocked_for'])
        blocked_text = f' · blocked {blocked:.1f}s' if blocked > 0.0 else ''
        self.summary_labels['limits'].setText(f"REST {rest_used}/{rest_limit} · WS {ws_used}/{ws_limit} · orders {order_used}/{order_limit} · connections {limiter['connections_5m']}/{limiter['connection_limit_5m']}{blocked_text}")
        self.summary_labels['render'].setText(f'chart {chart_rate:.0f} paints/s · book {book_rate:.0f} paints/s · book data {book_updates:.0f}/s')
        dom_state_getter = getattr(getattr(self.host, 'orderbook', None), 'performance_state', None)
        if callable(dom_state_getter):
            dom = dom_state_getter()
            received = int(dom.get('snapshots_received', 0))
            row_superseded = int(dom.get('row_snapshots_superseded', 0))
            row_superseded_pct = 100.0 * row_superseded / received if received > 0 else 0.0
            cache_hits = int(dom.get('text_cache_hits', 0))
            cache_misses = int(dom.get('text_cache_misses', 0))
            cache_total = cache_hits + cache_misses
            cache_hit_pct = 100.0 * cache_hits / cache_total if cache_total > 0 else 0.0
            self.summary_labels['dom'].setText(f"{float(dom.get('actual_fps', 0.0)):.0f} fps (fast {float(dom.get('fast_paints_per_second', 0.0)):.0f} · full {float(dom.get('full_paints_per_second', 0.0)):.0f} · partial {float(dom.get('partial_paints_per_second', 0.0)):.0f}) · build p95 {float(dom.get('snapshot_build_p95_ms', 0.0)):.2f} ms · prepare {float(dom.get('last_prepare_ms', 0.0)):.1f} ms · paint {float(dom.get('last_paint_ms', 0.0)):.1f} ms · E2E {float(dom.get('last_end_to_end_ms', 0.0)):.1f} ms · row superseded {row_superseded_pct:.0f}% · text cache {cache_hit_pct:.0f}% {int(dom.get('text_cache_entries', 0))}/4096 ev {int(dom.get('text_cache_evictions', 0))} · cols {str(dom.get('visible_analytic_columns', '—')) or '—'}")
        else:
            self.summary_labels['dom'].setText('—')
        depth_cost = self._timing_profile(timings.get('analysis.orderflow_add_depth_ms', ()))
        trade_cost = self._timing_profile(timings.get('analysis.orderflow_add_trade_ms', ()))
        book_ticker_cost = self._timing_profile(timings.get('analysis.orderflow_add_book_ticker_ms', ()), precision=3)
        snapshot_cost = self._timing_profile(timings.get('render.orderflow_snapshot_build_ms', ()))
        scheduler_cost = self._timing_profile(timings.get('render.orderflow_dirty_to_snapshot_ms', ()))
        self.summary_labels['orderflow_cost'].setText(f'depth {depth_cost} · trade {trade_cost} (1/16) · BBO {book_ticker_cost} (1/64) · snapshot {snapshot_cost} · dirty→snapshot {scheduler_cost}')
        cache_rebuild_cost = self._timing_profile(timings.get('analysis.orderflow_level_cache_rebuild_ms', ()))
        cache_hit_cost = self._timing_profile(timings.get('analysis.orderflow_level_cache_hit_ms', ()), precision=3)
        order_flow_state_getter = getattr(self.host, 'order_flow_diagnostic_state', None)
        if callable(order_flow_state_getter):
            order_flow_state = order_flow_state_getter()
            self.summary_labels['orderflow_cache'].setText(f"rebuild {cache_rebuild_cost} · hit {cache_hit_cost} (1/16 sampled) · counts R{int(order_flow_state.get('snapshot_level_cache_rebuilds', 0))} H{int(order_flow_state.get('snapshot_level_cache_hits', 0))}")
            self.summary_labels['orderflow_state'].setText(f"levels {int(order_flow_state.get('level_states', 0))}/2048 · book {int(order_flow_state.get('current_bid_levels', 0))}/{int(order_flow_state.get('current_ask_levels', 0))} · pending exec {int(order_flow_state.get('pending_executions', 0))} · cancel {int(order_flow_state.get('pending_cancellations', 0))} · buckets T{int(order_flow_state.get('trade_buckets', 0))} L{int(order_flow_state.get('liquidity_buckets', 0))}")
        else:
            self.summary_labels['orderflow_cache'].setText(f'rebuild {cache_rebuild_cost} · hit {cache_hit_cost} (1/16 sampled)')
            self.summary_labels['orderflow_state'].setText('—')
        micro_depth = self._timing_profile(timings.get('analysis.microstructure_add_depth_ms', ()))
        micro_trade = self._timing_profile(timings.get('analysis.microstructure_add_trade_ms', ()))
        micro_eval = self._timing_profile(timings.get('analysis.microstructure_evaluate_ms', ()))
        self.summary_labels['microstructure_cost'].setText(f'depth {micro_depth} · trade {micro_trade} (1/16 sampled) · evaluation {micro_eval}')
        aggregation_parts = []
        for multiplier in (1, 2, 5, 10, 25, 50):
            values = timings.get(f'render.orderflow_aggregation_{multiplier}x_ms', ())
            if values:
                aggregation_parts.append(f'{multiplier}× {self._timing_profile(values, precision=3)}')
        if aggregation_parts:
            self.summary_labels['aggregation_cost'].setText(' · '.join(aggregation_parts))
        else:
            self.summary_labels['aggregation_cost'].setText('No samples yet · use each aggregation level briefly to populate measurements')
        publish_cost = self._timing_profile(timings.get('depth.worker_publish_ms', ()))
        worker_gui = self._timing_profile(timings.get('depth.worker_to_gui_ms', ()))
        parser_age = self._timing_profile(timings.get('depth.parser_queue_age_ms', ()))
        rest_cost = self._timing_profile(timings.get('depth.snapshot_rest_ms', ()))
        bridge_cost = self._timing_profile(timings.get('depth.snapshot_bridge_ms', ()))
        recovery_cost = self._timing_profile(timings.get('depth.resync_to_ready_ms', ()))
        resync_causes = []
        prefix = 'depth.resync.reason.'
        for key, value in sorted(counters.items()):
            if key.startswith(prefix) and int(value) > 0:
                resync_causes.append(f'{key[len(prefix):]} {int(value)}')
        cause_text = ', '.join(resync_causes) if resync_causes else 'none'
        self.summary_labels['depth_pipeline'].setText(f"publish {publish_cost} · worker→GUI {worker_gui} · parser age {parser_age} · REST snapshot {rest_cost} · bridge {bridge_cost} · recovery {recovery_cost} · pub {rate('depth.worker_publications'):.1f}/s · resync {total('depth.resync.requested')} ({cause_text})")
        self.summary_labels['health'].setText(f"errors {total('errors.total')} · warnings {total('warnings.total')} · REST errors {total('rest.errors')} · slow {total('rest.slow')} · reconnects {total('ws.reconnects')}")
        state_getter = getattr(self.host.chart, 'diagnostic_state', None)
        if callable(state_getter):
            state = state_getter()
            width, height = state.get('size', (0, 0))
            self.summary_labels['renderer'].setText(f"{state.get('renderer', '—')} · {state.get('viewport', '—')} · {state.get('viewport_update_mode', '—')} · DPR {state.get('device_pixel_ratio', 1.0):.2f} · {width}×{height} · {('ready' if state.get('ready') else 'not ready')}")
            render_path = state.get('render_path', {})

            def chart_rate(key: str) -> float:
                current = int(render_path.get(key, 0))
                previous = int(self._last_chart_metrics.get(key, current))
                return max(0.0, (current - previous) / elapsed)
            native_rate = chart_rate('native_draws')
            gpu_path_detail = dict(render_path)
            for key in ('requested_opengl', 'requested_native', 'actual_render_path', 'gpu_status', 'runtime_fallback_reason', 'gpu_batches'):
                if key in state:
                    gpu_path_detail[key] = state[key]
            gpu_path_detail.setdefault('actual_viewport', state.get('viewport', ''))
            gpu_path_detail.setdefault('requested_context_format', state.get('requested_context_format', ''))
            self.summary_labels['chart_path'].setText(self._chart_gpu_detail(gpu_path_detail, native_rate))
            self._last_chart_metrics = {key: int(render_path.get(key, 0)) for key in ('native_draws', 'native_failures', 'fallback_frames', 'cpu_paints')}
        else:
            self.summary_labels['renderer'].setText('—')
            self.summary_labels['chart_path'].setText('—')
        self.summary_labels['lag'].setText(f'{self._lag_ms:.0f} ms current · {self._peak_lag_ms:.0f} ms peak')
        endpoints = [(key.split('::', 1)[1], int(value)) for key, value in counters.items() if key.startswith('rest.endpoint::')]
        endpoints.sort(key=lambda item: (-item[1], item[0]))
        self.endpoint_label.setText(' · '.join((f'{name}  {count}' for name, count in endpoints[:8])) if endpoints else 'No REST requests recorded yet.')
        self._refresh_log(snapshot['events'])
        self._last_counters = dict(counters)
        self._last_refresh = now

    def _refresh_log(self, events: list[tuple[float, int, str, str, str]]) -> None:
        threshold = int(self.verbosity.currentIndex())
        signature = (threshold, len(events), events[-1] if events else None)
        if signature == self._last_log_signature:
            return
        self._last_log_signature = signature
        filtered = [event for event in events if event[1] <= threshold][-180:]
        self.log_table.setUpdatesEnabled(False)
        self.log_table.setRowCount(len(filtered))
        for row, (stamp, _verbosity, kind, category, message) in enumerate(filtered):
            values = (time.strftime('%H:%M:%S', time.localtime(stamp)), kind, category, message)
            for column, value in enumerate(values):
                self.log_table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
        self.log_table.setUpdatesEnabled(True)
        if filtered:
            self.log_table.scrollToBottom()


import os
import threading
from datetime import datetime, timezone

from PySide6.QtCore import QUrl, Signal

from ..constants import HISTORY_PAGE_LIMIT, INTERVAL_SECONDS, MAX_CHART_CANDLES
from ..database import AppDatabase
from ..models import Candle, human_number
from ..networking.binance import BinanceRest, BinanceVisionArchive
from ..networking.binance import ApiTask

class HistoryDownloadDialog(QtWidgets.QDialog):
    def __init__(
        self,
        symbol: str,
        interval: str,
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Download historical candles")
        self.setMinimumWidth(470)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel(f"{symbol}  |  {interval.upper()}")
        heading.setObjectName("dialogHeading")
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)
        self.range_combo = QtWidgets.QComboBox()
        for label, days in (
            ("Previous 7 days", 7),
            ("Previous 30 days", 30),
            ("Previous 90 days", 90),
            ("Previous year", 365),
            ("All available Binance history", 0),
        ):
            self.range_combo.addItem(label, days)
        self.range_combo.setCurrentIndex(2)
        self.save_csv = QtWidgets.QCheckBox("Save a reusable CSV copy")
        self.note = QtWidgets.QLabel()
        self.note.setObjectName("subtleLabel")
        self.note.setWordWrap(True)
        self.range_combo.currentIndexChanged.connect(self._range_changed)
        form.addRow("Range", self.range_combo)
        form.addRow("", self.save_csv)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("DOWNLOAD")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(heading)
        layout.addLayout(form)
        layout.addWidget(self.note)
        layout.addWidget(buttons)
        self._range_changed()

    def _range_changed(self) -> None:
        all_history = self.range_combo.currentData() == 0
        if all_history:
            self.save_csv.setChecked(True)
        self.save_csv.setEnabled(not all_history)
        self.note.setText(
            "All candles are retained in the local research database and written to CSV; "
            f"the live chart renders up to the newest {MAX_CHART_CANDLES:,} candles for responsive navigation."
            if all_history
            else "The download runs in the background, persists locally, and merges into the current chart."
        )

    def options(self) -> tuple[int, bool]:
        return int(self.range_combo.currentData()), self.save_csv.isChecked()


class MarketDataOptionsDialog(QtWidgets.QDialog):
    """Choose reusable market-history datasets without simulation-specific semantics."""

    def __init__(self, active_count: int, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Market history data")
        self.setMinimumWidth(500)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel("MARKET HISTORY DATA")
        heading.setObjectName("dialogHeading")
        note = QtWidgets.QLabel(
            f"Download reusable history for all {active_count:,} active USDT perpetuals. "
            "Completed pages are cached locally and can be resumed later."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        self.checks: dict[str, QtWidgets.QCheckBox] = {}
        box = QtWidgets.QGroupBox("Datasets")
        grid = QtWidgets.QGridLayout(box)
        for index, (key, label) in enumerate((
            ("candles", "15m candles + derived 1h / 4h / 1D"),
            ("funding", "Funding history"),
            ("premium_index", "Premium-index history"),
            ("open_interest", "Open-interest history"),
        )):
            checkbox = QtWidgets.QCheckBox(label)
            checkbox.setChecked(True)
            self.checks[key] = checkbox
            grid.addWidget(checkbox, index // 2, index % 2)
        warning = QtWidgets.QLabel(
            "Binance Vision is used for OI and premium backfills. The newest gap is "
            "filled from Binance REST under the application's global safety limiter."
        )
        warning.setObjectName("subtleLabel")
        warning.setWordWrap(True)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("DOWNLOAD")
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(heading)
        layout.addWidget(note)
        layout.addWidget(box)
        layout.addWidget(warning)
        layout.addWidget(buttons)

    def _accept_validated(self) -> None:
        if not any(box.isChecked() for box in self.checks.values()):
            QtWidgets.QMessageBox.information(self, "Choose data", "Select at least one dataset to download.")
            return
        self.accept()

    def datasets(self) -> set[str]:
        return {key for key, box in self.checks.items() if box.isChecked()}


class MarketHistoryDownloadDialog(QtWidgets.QDialog):
    status_pending = Signal()
    symbol_changed = Signal(str, int, int)
    safe_to_close = Signal()

    def __init__(
        self,
        database: AppDatabase,
        rest: BinanceRest,
        symbols: list[str],
        datasets: set[str] | None = None,
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self.database = database
        self.rest = rest
        self.symbols = list(dict.fromkeys(symbol for symbol in symbols if symbol.endswith("USDT")))
        self.datasets = set(datasets or {"candles", "funding", "premium_index", "open_interest"})
        self.vision = BinanceVisionArchive(database)
        self.stop_event = threading.Event()
        self.task: ApiTask | None = None
        self._status_lock = threading.Lock()
        self._pending_status_text = ""
        self._status_wake_pending = False
        self._status_last_applied_mono = 0.0
        self.setWindowTitle("Download complete USD-M research history")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(660)
        self.setModal(False)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel("ALL ACTIVE USDT PERPETUALS")
        heading.setObjectName("dialogHeading")
        note = QtWidgets.QLabel(
            "Selected research datasets are checkpointed after every complete page or archive. "
            "Restarting this downloader resumes without repeating completed work. "
            "Live feeds and trading remain available; downloads use background request priority."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        self.current = QtWidgets.QLabel("Preparing market queue...")
        self.current.setObjectName("workspaceHeading")
        self.detail = QtWidgets.QLabel("No request sent yet.")
        self.detail.setObjectName("subtleLabel")
        self.detail.setWordWrap(True)
        self.overall = QtWidgets.QProgressBar()
        self.overall.setRange(0, max(1, len(self.symbols)))
        self.page = QtWidgets.QProgressBar()
        self.page.setRange(0, 0)
        self.page.setFormat("Current market  |  page-level checkpointing")
        controls = QtWidgets.QHBoxLayout()
        self.pause_button = QtWidgets.QPushButton("PAUSE SAFELY")
        self.pause_button.setToolTip(
            "Finish and save the current API page, then stop. Start this downloader again to resume."
        )
        self.close_button = QtWidgets.QPushButton("CLOSE")
        self.close_button.setEnabled(False)
        controls.addStretch(1)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.close_button)
        layout.addWidget(heading)
        layout.addWidget(note)
        layout.addWidget(self.current)
        layout.addWidget(self.detail)
        layout.addWidget(self.overall)
        layout.addWidget(self.page)
        layout.addLayout(controls)
        self.pause_button.clicked.connect(self.request_pause)
        self.close_button.clicked.connect(self.accept)
        self.status_pending.connect(self._schedule_status_flush)
        self._status_flush_timer = QTimer(self)
        self._status_flush_timer.setSingleShot(True)
        self._status_flush_timer.timeout.connect(self._flush_pending_status)
        self.symbol_changed.connect(self._show_symbol)
        QTimer.singleShot(0, self._start)

    def _queue_status(self, text: str) -> None:
        if self.stop_event.is_set():
            return
        text = str(text)
        with self._status_lock:
            self._pending_status_text = text
            if self._status_wake_pending:
                return
            self._status_wake_pending = True
        self.status_pending.emit()

    def _schedule_status_flush(self) -> None:
        elapsed_ms = (time.monotonic() - self._status_last_applied_mono) * 1000.0
        remaining_ms = max(0, 125 - int(elapsed_ms))
        if remaining_ms <= 0:
            self._flush_pending_status()
        elif not self._status_flush_timer.isActive():
            self._status_flush_timer.start(remaining_ms)

    def _flush_pending_status(self) -> None:
        with self._status_lock:
            text = self._pending_status_text
            self._pending_status_text = ""
            self._status_wake_pending = False
        if not text:
            return
        self.detail.setText(text)
        self._status_last_applied_mono = time.monotonic()

    def _clear_pending_status(self) -> None:
        if self._status_flush_timer.isActive():
            self._status_flush_timer.stop()
        with self._status_lock:
            self._pending_status_text = ""
            self._status_wake_pending = False

    def _apply_status_immediately(self, text: str) -> None:
        self._clear_pending_status()
        self.detail.setText(str(text))
        self._status_last_applied_mono = time.monotonic()

    def _show_symbol(self, symbol: str, index: int, total: int) -> None:
        self.current.setText(f"{symbol}  |  MARKET {index} OF {total}")
        self.overall.setValue(max(0, index - 1))

    def request_pause(self) -> None:
        if self.task is None:
            return
        self.stop_event.set()
        self.pause_button.setEnabled(False)
        self.pause_button.setText("PAUSING AFTER PAGE...")
        self._apply_status_immediately(
            "Pause requested  |  saving the current complete API page before stopping."
        )

    def _start(self) -> None:
        if not self.symbols:
            self.detail.setText("No active USDT perpetuals were available.")
            self.close_button.setEnabled(True)
            return
        task: ApiTask

        def work() -> dict[str, Any]:
            interval = "15m"
            interval_ms = INTERVAL_SECONDS[interval] * 1000
            end_ms = int(time.time() * 1000)
            end_ms = end_ms - end_ms % interval_ms - interval_ms
            completed = 0
            paused_at = ""
            for index, symbol in enumerate(self.symbols, 1):
                if self.stop_event.is_set():
                    paused_at = symbol
                    break
                self.symbol_changed.emit(symbol, index, len(self.symbols))
                self._queue_status("Discovering listing time and local candle coverage...")
                first_page = self.rest.get(
                    "/fapi/v1/klines",
                    {"symbol": symbol, "interval": interval, "startTime": 0, "limit": 1},
                    priority="background",
                )
                if not first_page:
                    self._queue_status("No candle history returned  |  skipping market.")
                    continue
                listing_ms = int(first_page[0][0])
                if "candles" in self.datasets:
                    first_cached, last_cached, row_count = self.database.candle_coverage(
                        symbol, interval
                    )
                    coverage = self.database.download_coverage(symbol, interval)
                    expected_cached = (
                        (last_cached - first_cached) // interval_ms + 1
                        if first_cached and last_cached >= first_cached
                        else 0
                    )
                    contiguous_from_listing = bool(
                        first_cached <= listing_ms + interval_ms
                        and row_count >= expected_cached * 0.985
                    )
                    cursor = (
                        max(listing_ms, last_cached + interval_ms)
                        if bool(coverage.get("complete_from_listing"))
                        or contiguous_from_listing
                        else listing_ms
                    )
                    while cursor <= end_ms:
                        self._queue_status(
                            f"Candles - {datetime.fromtimestamp(cursor / 1000, timezone.utc):%Y-%m-%d} "
                            "- Binance-safe background rate"
                        )
                        rows = self.rest.get(
                            "/fapi/v1/klines",
                            {
                                "symbol": symbol,
                                "interval": interval,
                                "startTime": cursor,
                                "endTime": end_ms,
                                "limit": HISTORY_PAGE_LIMIT,
                            },
                            priority="background",
                        )
                        if not rows:
                            break
                        candles = [Candle.from_rest(row) for row in rows]
                        self.database.cache_candles(symbol, interval, candles)
                        last_open = int(rows[-1][0])
                        finished_candles = (
                            len(rows) < HISTORY_PAGE_LIMIT or last_open >= end_ms
                        )
                        self.database.mark_download_coverage(
                            symbol,
                            interval,
                            listing_ms,
                            last_open,
                            finished_candles,
                        )
                        cursor = last_open + interval_ms
                        if self.stop_event.is_set() or finished_candles:
                            break
                    if self.stop_event.is_set():
                        paused_at = symbol
                        break
                    self._queue_status(
                        "Deriving UTC-aligned 1h, 4h and 1D candles locally..."
                    )
                    self.database.aggregate_candle_intervals(
                        symbol,
                        interval,
                        ("1h", "4h", "1d"),
                    )

                if "funding" in self.datasets and not self.stop_event.is_set():
                    self._queue_status(
                        "Funding history - resuming from the last committed page..."
                    )
                    funding_coverage = self.database.event_download_coverage(
                        symbol, "funding"
                    )
                    funding_cursor = max(
                        listing_ms,
                        int(funding_coverage.get("last_time", 0)) + 1,
                    )
                    while funding_cursor <= end_ms:
                        page = self.rest.get(
                            "/fapi/v1/fundingRate",
                            {
                                "symbol": symbol,
                                "startTime": funding_cursor,
                                "endTime": end_ms,
                                "limit": 1000,
                            },
                            priority="background",
                        )
                        if not page:
                            break
                        event_rows = [
                            (int(row.get("fundingTime", 0)), dict(row))
                            for row in page
                            if int(row.get("fundingTime", 0)) > 0
                        ]
                        complete = len(page) < 1000
                        self.database.cache_market_event_page(
                            symbol, "funding", event_rows, complete
                        )
                        funding_cursor = int(page[-1].get("fundingTime", 0)) + 1
                        if self.stop_event.is_set() or complete:
                            break

                if "premium_index" in self.datasets and not self.stop_event.is_set():
                    self._queue_status(
                        "Premium index - importing missing Binance Vision archives..."
                    )
                    self.vision.import_premium_index(
                        symbol,
                        self.stop_event.is_set,
                        self._queue_status,
                    )

                if "open_interest" in self.datasets and not self.stop_event.is_set():
                    self._queue_status(
                        "Open interest - importing missing Binance Vision archives..."
                    )
                    self.vision.import_open_interest(
                        symbol,
                        self.stop_event.is_set,
                        self._queue_status,
                    )
                    if not self.stop_event.is_set():
                        self._queue_status(
                            "Open interest - filling the newest Binance REST gap..."
                        )
                        oldest_oi = max(listing_ms, end_ms - 31 * 86_400_000)
                        oi_coverage = self.database.event_download_coverage(
                            symbol, "open_interest"
                        )
                        oi_cursor = max(
                            oldest_oi,
                            int(oi_coverage.get("last_time", 0)) + 1,
                        )
                        while oi_cursor <= end_ms:
                            page = self.rest.get(
                                "/futures/data/openInterestHist",
                                {
                                    "symbol": symbol,
                                    "period": "15m",
                                    "startTime": oi_cursor,
                                    "endTime": end_ms,
                                    "limit": 500,
                                },
                                priority="background",
                            )
                            if not page:
                                break
                            event_rows = [
                                (int(row.get("timestamp", 0)), dict(row))
                                for row in page
                                if int(row.get("timestamp", 0)) > 0
                            ]
                            self.database.cache_market_event_page(
                                symbol, "open_interest", event_rows, False
                            )
                            oi_cursor = int(page[-1].get("timestamp", 0)) + 1
                            if self.stop_event.is_set() or len(page) < 500:
                                break
                if self.stop_event.is_set():
                    paused_at = symbol
                    break
                completed += 1
                task.signals.progress.emit(index, len(self.symbols))
            return {
                "completed": completed,
                "total": len(self.symbols),
                "paused": self.stop_event.is_set(),
                "paused_at": paused_at,
            }

        def done(result: dict[str, Any]) -> None:
            self.task = None
            self._clear_pending_status()
            self.page.setRange(0, 1)
            self.page.setValue(1)
            self.overall.setValue(
                min(len(self.symbols), int(result.get("completed", 0)))
            )
            if result.get("paused"):
                self.current.setText("DOWNLOAD PAUSED SAFELY")
                self.detail.setText(
                    f"Completed pages are saved. Run this downloader again to resume"
                    + (f" at {result.get('paused_at')}." if result.get("paused_at") else ".")
                )
            else:
                self.current.setText("MARKET HISTORY READY")
                self.detail.setText(
                    f"Updated {result.get('completed', 0)} of {result.get('total', 0)} active USDT perpetuals."
                )
            self.pause_button.setVisible(False)
            self.close_button.setEnabled(True)
            self.safe_to_close.emit()

        def failed(message: str) -> None:
            self.task = None
            self._clear_pending_status()
            self.page.setRange(0, 1)
            self.page.setValue(0)
            self.current.setText("DOWNLOAD STOPPED")
            self.detail.setText(
                f"{message}\nCompleted pages remain saved; start again to resume."
            )
            self.pause_button.setVisible(False)
            self.close_button.setEnabled(True)
            self.safe_to_close.emit()

        task = ApiTask(work)
        task.signals.progress.connect(lambda complete, total: self.overall.setValue(complete))
        task.signals.finished.connect(done)
        task.signals.failed.connect(failed)
        self.task = task
        QtCore.QThreadPool.globalInstance().start(task)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.task is not None:
            self.request_pause()
            event.ignore()
            return
        event.accept()


class DataCacheDialog(QtWidgets.QDialog):
    def __init__(self, database: AppDatabase, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.database = database
        self.setWindowTitle("Nightwatch data cache")
        self.setMinimumWidth(520)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel("PERSISTENT MARKET DATA")
        heading.setObjectName("dialogHeading")
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        path = QtWidgets.QLineEdit(database.path)
        path.setReadOnly(True)
        note = QtWidgets.QLabel(
            "Downloaded candles and recorded derivatives feeds stay on this computer. "
            "Cleanup below is explicit and cannot be undone."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        self.candles = QtWidgets.QCheckBox("Downloaded candle cache")
        self.events = QtWidgets.QCheckBox("Recorded funding / premium / OI / depth / liquidation events")
        actions = QtWidgets.QHBoxLayout()
        open_button = QtWidgets.QPushButton("OPEN DATA FOLDER")
        delete_button = QtWidgets.QPushButton("DELETE SELECTED...")
        delete_button.setObjectName("dangerButton")
        close_button = QtWidgets.QPushButton("CLOSE")
        open_button.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(database.path))))
        delete_button.clicked.connect(self._delete)
        close_button.clicked.connect(self.accept)
        actions.addWidget(open_button)
        actions.addStretch(1)
        actions.addWidget(delete_button)
        actions.addWidget(close_button)
        layout.addWidget(heading)
        layout.addWidget(self.summary)
        layout.addWidget(path)
        layout.addWidget(note)
        layout.addWidget(self.candles)
        layout.addWidget(self.events)
        layout.addLayout(actions)
        self._refresh()

    def _refresh(self) -> None:
        summary = self.database.storage_summary()
        self.summary.setText(
            f"{summary['candles']:,} candles   |   {summary['events']:,} market events   |   "
            f"{human_number(summary['bytes'])}B on disk"
        )

    def _delete(self) -> None:
        selected = (self.candles.isChecked(), self.events.isChecked())
        if not any(selected):
            QtWidgets.QMessageBox.information(self, "Select data", "Select at least one data group to delete.")
            return
        answer = QtWidgets.QMessageBox.warning(
            self, "Delete selected market data?",
            "This permanently removes the selected local database records. Continue?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.database.clear_storage(*selected)
        self.candles.setChecked(False)
        self.events.setChecked(False)
        self._refresh()
