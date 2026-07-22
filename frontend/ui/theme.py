COLORS = {
    "primary":        "#1A3A5C",
    "primary_dark":   "#0F2540",
    "primary_light":  "#2E5F8A",
    "secondary":      "#C9A227",
    "secondary_dark": "#A07D1A",
    "bg_main":        "#F4F6F9",
    "bg_white":       "#FFFFFF",
    "bg_sidebar":     "#1A3A5C",
    "bg_card":        "#FFFFFF",
    "text_primary":   "#1A1A2E",
    "text_secondary": "#4A5568",
    "text_muted":     "#718096",
    "text_light":     "#FFFFFF",
    "success":        "#2E7D32",
    "success_bg":     "#E8F5E9",
    "warning":        "#E65100",
    "warning_bg":     "#FFF3E0",
    "error":          "#C62828",
    "error_bg":       "#FFEBEE",
    "border":         "#CBD5E0",
    "border_focus":   "#C9A227",
    "row_alt":        "#F7F9FC",
    "row_hover":      "#EDF2F7",
    "sidebar_hover":  "#2E5F8A",
    "sidebar_active": "#C9A227",
}

FONTS = {
    "family":  "Segoe UI",
    "mono":    "Consolas",
    "size_xs": 9,
    "size_sm": 10,
    "size_md": 11,
    "size_lg": 13,
    "size_xl": 16,
    "size_2xl": 20,
    "size_3xl": 26,
}

SPACING = {
    "xs":  4,
    "sm":  8,
    "md":  12,
    "lg":  16,
    "xl":  24,
    "2xl": 32,
}


def get_app_stylesheet() -> str:
    p = COLORS["primary"]
    pd_ = COLORS["primary_dark"]
    pl = COLORS["primary_light"]
    s = COLORS["secondary"]
    sd = COLORS["secondary_dark"]
    bg = COLORS["bg_main"]
    white = COLORS["bg_white"]
    tp = COLORS["text_primary"]
    ts = COLORS["text_secondary"]
    tm = COLORS["text_muted"]
    border = COLORS["border"]
    bf = COLORS["border_focus"]
    err = COLORS["error"]

    return f"""
    QWidget {{
        font-family: "{FONTS['family']}";
        font-size: {FONTS['size_md']}pt;
        color: {tp};
    }}

    QMainWindow, QDialog {{
        background-color: {bg};
    }}

    /* ── Sidebar ────────────────────────────── */
    #Sidebar {{
        background-color: {p};
        border-right: 1px solid {pd_};
    }}

    #SidebarLogo {{
        background-color: {pd_};
        color: {white};
        font-size: {FONTS['size_xl']}pt;
        font-weight: bold;
        padding: 20px 16px;
        border-bottom: 2px solid {s};
    }}

    #SidebarSection {{
        color: {s};
        font-size: {FONTS['size_xs']}pt;
        font-weight: bold;
        padding: 12px 16px 4px 16px;
        letter-spacing: 1px;
        text-transform: uppercase;
    }}

    #NavButton {{
        background-color: transparent;
        color: rgba(255,255,255,0.80);
        border: none;
        text-align: left;
        padding: 10px 20px;
        font-size: {FONTS['size_sm']}pt;
        border-radius: 0;
    }}
    #NavButton:hover {{
        background-color: {COLORS['sidebar_hover']};
        color: {white};
    }}
    #NavButton[active="true"] {{
        background-color: rgba(201,162,39,0.18);
        color: {s};
        border-left: 3px solid {s};
    }}

    /* ── Content area ─────────────────────── */
    #ContentArea {{
        background-color: {bg};
    }}

    #PageTitle {{
        font-size: {FONTS['size_xl']}pt;
        font-weight: bold;
        color: {p};
        padding: 0 0 4px 0;
    }}

    #PageSubtitle {{
        font-size: {FONTS['size_sm']}pt;
        color: {ts};
    }}

    #PageHeader {{
        background-color: {white};
        border-bottom: 1px solid {border};
        padding: 16px 24px;
    }}

    /* ── Cards ────────────────────────────── */
    #Card {{
        background-color: {white};
        border: 1px solid {border};
        border-radius: 4px;
    }}

    #CardHeader {{
        background-color: {p};
        color: {white};
        font-size: {FONTS['size_sm']}pt;
        font-weight: bold;
        padding: 8px 14px;
        border-radius: 3px 3px 0 0;
        letter-spacing: 0.5px;
    }}

    /* ── Inputs ───────────────────────────── */
    QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox {{
        background-color: {white};
        border: 1px solid {border};
        border-radius: 3px;
        padding: 6px 10px;
        font-size: {FONTS['size_sm']}pt;
        color: {tp};
        selection-background-color: {pl};
    }}
    QLineEdit:focus, QComboBox:focus, QDateEdit:focus {{
        border: 1.5px solid {bf};
        outline: none;
    }}
    QLineEdit:disabled, QLineEdit[readOnly="true"] {{
        background-color: #EDF2F7;
        color: {ts};
    }}
    QLineEdit#invalid {{
        border: 1.5px solid {err};
        background-color: #FFF5F5;
    }}

    QComboBox::drop-down {{
        border: none;
        padding-right: 8px;
    }}
    QComboBox QAbstractItemView {{
        border: 1px solid {border};
        selection-background-color: {p};
        selection-color: {white};
    }}

    /* ── Buttons ──────────────────────────── */
    QPushButton {{
        background-color: {p};
        color: {white};
        border: none;
        border-radius: 3px;
        padding: 8px 20px;
        font-size: {FONTS['size_sm']}pt;
        font-weight: bold;
        letter-spacing: 0.5px;
    }}
    QPushButton:hover {{
        background-color: {pl};
    }}
    QPushButton:pressed {{
        background-color: {pd_};
    }}
    QPushButton:disabled {{
        background-color: {border};
        color: {tm};
    }}

    QPushButton#SecondaryBtn {{
        background-color: {pl};
        color: {white};
        border: none;
    }}
    QPushButton#SecondaryBtn:hover {{
        background-color: {p};
        color: {white};
    }}
    QPushButton#SecondaryBtn:pressed {{
        background-color: {pd_};
    }}

    QPushButton#DangerBtn {{
        background-color: {err};
        color: {white};
    }}
    QPushButton#DangerBtn:hover {{
        background-color: #B71C1C;
    }}

    QPushButton#GoldBtn {{
        background-color: {s};
        color: {white};
    }}
    QPushButton#GoldBtn:hover {{
        background-color: {sd};
    }}

    QPushButton#ConfirmBtn {{
        background-color: {p};
        color: {white};
        font-size: {FONTS['size_md']}pt;
        padding: 10px 32px;
    }}
    QPushButton#ConfirmBtn:hover {{
        background-color: {pl};
    }}
    QPushButton#ConfirmBtn:pressed {{
        background-color: {pd_};
    }}

    /* ── Tables ───────────────────────────── */
    QTableWidget, QTableView {{
        background-color: {white};
        gridline-color: {border};
        border: 1px solid {border};
        border-radius: 3px;
        selection-background-color: {COLORS['row_hover']};
        selection-color: {tp};
        alternate-background-color: {COLORS['row_alt']};
    }}
    QTableWidget::item, QTableView::item {{
        padding: 6px 10px;
        border: none;
    }}
    QTableWidget::item:selected, QTableView::item:selected {{
        background-color: {COLORS['row_hover']};
        color: {tp};
    }}
    QHeaderView::section {{
        background-color: {p};
        color: {white};
        padding: 8px 10px;
        border: none;
        border-right: 1px solid {pl};
        font-size: {FONTS['size_xs']}pt;
        font-weight: bold;
        letter-spacing: 0.5px;
    }}
    QHeaderView::section:last {{
        border-right: none;
    }}

    /* ── Scrollbars ───────────────────────── */
    QScrollBar:vertical {{
        background: {bg};
        width: 10px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {border};
        border-radius: 5px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {ts};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

    /* ── Labels ───────────────────────────── */
    #FieldLabel {{
        font-size: {FONTS['size_xs']}pt;
        font-weight: bold;
        color: {ts};
        letter-spacing: 0.3px;
        text-transform: uppercase;
    }}

    #ValueLabel {{
        font-size: {FONTS['size_sm']}pt;
        color: {tp};
        background-color: #EDF2F7;
        padding: 5px 10px;
        border-radius: 2px;
    }}

    #TotalBox {{
        background-color: {p};
        color: {white};
        font-size: {FONTS['size_2xl']}pt;
        font-weight: bold;
        padding: 12px 20px;
        border-radius: 4px;
    }}

    #StatusBalanced {{
        background-color: {COLORS['success_bg']};
        color: {COLORS['success']};
        font-size: {FONTS['size_2xl']}pt;
        font-weight: bold;
        padding: 16px;
        border: 2px solid {COLORS['success']};
        border-radius: 6px;
    }}

    #StatusNotBalanced {{
        background-color: {COLORS['warning_bg']};
        color: {COLORS['warning']};
        font-size: {FONTS['size_2xl']}pt;
        font-weight: bold;
        padding: 16px;
        border: 2px solid {COLORS['warning']};
        border-radius: 6px;
    }}

    #ErrorMsg {{
        color: {err};
        font-size: {FONTS['size_xs']}pt;
        padding: 2px 0;
    }}

    /* ── Tab widget ───────────────────────── */
    QTabWidget::pane {{
        border: 1px solid {border};
        border-top: 2px solid {p};
    }}
    QTabBar::tab {{
        background-color: #E2E8F0;
        color: {ts};
        padding: 8px 18px;
        border: none;
        margin-right: 2px;
        font-size: {FONTS['size_sm']}pt;
    }}
    QTabBar::tab:selected {{
        background-color: {p};
        color: {white};
        font-weight: bold;
    }}
    QTabBar::tab:hover {{
        background-color: {border};
    }}

    /* ── Group boxes ──────────────────────── */
    QGroupBox {{
        border: 1px solid {border};
        border-radius: 3px;
        margin-top: 20px;
        padding-top: 8px;
        font-size: {FONTS['size_sm']}pt;
        font-weight: bold;
        color: {p};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        padding: 0 6px;
        color: {p};
    }}

    /* ── Separator ────────────────────────── */
    QFrame#Separator {{
        background-color: {border};
        max-height: 1px;
        min-height: 1px;
    }}

    /* ── Status bar ───────────────────────── */
    QStatusBar {{
        background-color: {pd_};
        color: rgba(255,255,255,0.8);
        font-size: {FONTS['size_xs']}pt;
        padding: 0 8px;
    }}
    """
