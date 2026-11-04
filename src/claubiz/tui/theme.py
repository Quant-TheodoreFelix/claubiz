from textual.theme import Theme

CLAY = "#D77757"
CLAY_SOFT = "#E8A68C"
PERIWINKLE = "#B1B9F9"
SUCCESS = "#4EBA65"
WARNING = "#E5B567"
ERROR = "#FF6B80"
MUTED = "#8E8A86"
FAINT = "#5C5855"
INK = "#1A1A1A"

_SHARED = {
    "text-muted": MUTED,
    "text-disabled": FAINT,
    "border": CLAY,
    "border-blurred": FAINT,
    "block-cursor-background": CLAY,
    "block-cursor-foreground": INK,
    "block-cursor-text-style": "bold",
    "block-cursor-blurred-background": "#3A3634",
    "block-cursor-blurred-foreground": "#E8E4DF",
    "block-cursor-blurred-text-style": "none",
    "block-hover-background": "#2E2B29",
    "input-cursor-background": CLAY,
    "input-cursor-foreground": INK,
    "input-cursor-text-style": "none",
    "input-selection-background": "#D7775766",
    "footer-key-foreground": CLAY,
    "footer-description-foreground": MUTED,
    "scrollbar": "#3A3634",
    "scrollbar-hover": FAINT,
    "scrollbar-active": CLAY,
    "link-color-hover": CLAY_SOFT,
    "markdown-h1-color": CLAY,
    "markdown-h1-background": "ansi_default",
    "markdown-h1-text-style": "bold",
    "markdown-h2-color": CLAY_SOFT,
    "markdown-h2-text-style": "bold",
    "markdown-h3-color": PERIWINKLE,
    "markdown-h3-text-style": "bold",
}

NATIVE = Theme(
    name="claubiz",
    primary=CLAY,
    secondary=PERIWINKLE,
    accent=CLAY,
    success=SUCCESS,
    warning=WARNING,
    error=ERROR,
    foreground="ansi_default",
    background="ansi_default",
    surface="ansi_default",
    panel="ansi_default",
    boost="ansi_default",
    dark=True,
    ansi=True,
    variables={
        **_SHARED,
        "ansi-background": "ansi_default",
        "ansi-foreground": "ansi_default",
        "footer-background": "ansi_default",
        "footer-key-background": "ansi_default",
        "footer-item-background": "ansi_default",
        "footer-description-background": "ansi_default",
        "scrollbar-background": "ansi_default",
        "scrollbar-background-hover": "ansi_default",
        "scrollbar-background-active": "ansi_default",
        "scrollbar-corner-color": "ansi_default",
        "screen-selection-background": "#D7775766",
    },
)

SOLID = Theme(
    name="claubiz-solid",
    primary=CLAY,
    secondary=PERIWINKLE,
    accent=CLAY,
    success=SUCCESS,
    warning=WARNING,
    error=ERROR,
    foreground="#E8E4DF",
    background="#1A1918",
    surface="#211F1E",
    panel="#2A2725",
    dark=True,
    variables={**_SHARED, "markdown-h1-background": "#1A1918"},
)

THEMES = {NATIVE.name: NATIVE, SOLID.name: SOLID}

STATUS_STYLES: dict[str, tuple[str, str, str]] = {
    "pending": ("○", "대기", FAINT),
    "queued": ("○", "대기열", MUTED),
    "running": ("●", "실행 중", CLAY),
    "done": ("●", "완료", SUCCESS),
    "reused": ("●", "재사용", PERIWINKLE),
    "failed": ("●", "실패", ERROR),
    "blocked": ("●", "예산 차단", ERROR),
    "skipped": ("○", "건너뜀", WARNING),
    "planned": ("○", "계획됨", MUTED),
    "rejected": ("○", "취소됨", FAINT),
    "cancelled": ("○", "중단됨", WARNING),
    "interrupted": ("○", "비정상 종료", WARNING),
    "completed": ("●", "완료", SUCCESS),
    "partial": ("◐", "일부 완료", WARNING),
}
