#include "ui.h"
#include "splash.h"
#include <lvgl.h>
#include "logo.h"
#include "icons.h"
#include "hal/board_caps.h"
#include "splash_animations.h"
#include <esp_heap_caps.h>

// Custom fonts (scaled for 314 PPI, ~1.9x from original 165 PPI)
LV_FONT_DECLARE(font_tiempos_56);
LV_FONT_DECLARE(font_tiempos_34);
LV_FONT_DECLARE(font_styrene_48);
LV_FONT_DECLARE(font_styrene_28);
LV_FONT_DECLARE(font_styrene_24);
LV_FONT_DECLARE(font_styrene_20);
LV_FONT_DECLARE(font_styrene_16);
LV_FONT_DECLARE(font_styrene_14);
LV_FONT_DECLARE(font_mono_32);

// Layout values computed from the active board's geometry. Populated once
// in ui_init() and treated as const for the rest of the program. Adding a
// new display size means extending compute_layout() with another
// breakpoint — never editing the screen-builder functions below.
struct Layout {
    int16_t scr_w, scr_h;
    int16_t margin;
    int16_t title_y;
    int16_t content_y;
    int16_t content_w;

    // Usage screen
    int16_t usage_panel_h;
    int16_t usage_panel_gap;
    int16_t usage_bar_y;
    int16_t usage_reset_y;

    // Bluetooth screen
    int16_t bt_info_panel_h;
    int16_t bt_reset_zone_h;
    const lv_font_t* bt_title_font;
    const lv_font_t* bt_status_font;
    const lv_font_t* bt_device_font;
    const lv_font_t* bt_credit_1_font;
    const lv_font_t* bt_credit_2_font;
};
static Layout L = {};

// Pick layout values from the active board's pixel dimensions. The two
// existing boards happen to land on the two breakpoints below; new ports
// inherit the closer one — visually OK, may need a polish pass for
// pixel-perfect alignment but never blocks the port from booting.
static void compute_layout(const BoardCaps& c) {
    L.scr_w = c.width;
    L.scr_h = c.height;
    L.margin = 20;
    L.title_y = 30;

    if (c.height >= 460) {
        // Large layout — tuned for 480x480 (AMOLED-2.16).
        L.content_y = 100;
        L.usage_panel_h = 150;
        L.usage_panel_gap = 16;
        L.usage_bar_y = 56;
        L.usage_reset_y = 94;
        L.bt_info_panel_h = 160;
        L.bt_reset_zone_h = 110;
        L.bt_title_font    = &font_tiempos_56;
        L.bt_status_font   = &font_styrene_48;
        L.bt_device_font   = &font_styrene_28;
        L.bt_credit_1_font = &font_styrene_24;
        L.bt_credit_2_font = &font_styrene_20;
    } else {
        // Compact layout — tuned for 368x448 (AMOLED-1.8).
        L.content_y = 85;
        L.usage_panel_h = 130;
        L.usage_panel_gap = 12;
        L.usage_bar_y = 48;
        L.usage_reset_y = 78;
        L.bt_info_panel_h = 140;
        L.bt_reset_zone_h = 90;
        L.bt_title_font    = &font_tiempos_34;
        L.bt_status_font   = &font_styrene_28;
        L.bt_device_font   = &font_styrene_20;
        L.bt_credit_1_font = &font_styrene_16;
        L.bt_credit_2_font = &font_styrene_14;
    }

    L.content_w = L.scr_w - 2 * L.margin;
}

// Anthropic brand palette — design tokens live in theme.h
#include "theme.h"
#define COL_BG        THEME_BG
#define COL_PANEL     THEME_PANEL
#define COL_TEXT      THEME_TEXT
#define COL_DIM       THEME_DIM
#define COL_ACCENT    THEME_ACCENT
#define COL_GREEN     THEME_GREEN
#define COL_AMBER     THEME_AMBER
#define COL_RED       THEME_RED
#define COL_BAR_BG    THEME_BAR_BG

// ---- Usage screen widgets (single non-splash view) ----
static lv_obj_t* usage_container;
static lv_obj_t* lbl_title;
static lv_obj_t* usage_group;   // the two usage panels — shown when connected
static lv_obj_t* pair_group;    // pairing hint — shown when disconnected
static lv_obj_t* bar_session;
static lv_obj_t* lbl_session_pct;
static lv_obj_t* lbl_session_label;
static lv_obj_t* lbl_session_reset;
static lv_obj_t* bar_weekly;
static lv_obj_t* lbl_weekly_pct;
static lv_obj_t* lbl_weekly_label;
static lv_obj_t* lbl_weekly_reset;
static lv_obj_t* lbl_anim;      // status line: connection state + whimsical idle

// ---- Battery indicator (shared, on top) ----
static lv_obj_t* battery_img;
static lv_obj_t* logo_img;
static lv_image_dsc_t battery_dscs[5];  // empty, low, medium, full, charging

// ---- Live-data freshness → which usage sub-view to show ----
// usage panels when data is flowing, an idle "Zzz" screen when the host is
// connected but no usage update landed within DATA_FRESH_MS, the pairing hint
// when BLE is down. Re-evaluated every loop in ui_tick_anim().
static lv_obj_t* idle_group;            // the "Zzz" idle screen
static uint32_t  last_data_ms = 0;      // lv_tick when the last valid usage update landed
static bool      data_received = false; // any valid update since boot
static int       view_state = -1;       // -1 unknown / 0 pair / 1 idle / 2 usage
static const uint32_t DATA_FRESH_MS = 90000;  // usage counts as "live" within this window (daemon sends ~60s)

// ---- Mood sprites (built at boot from splash animation palettes) ----
static uint8_t sprite_buf_working[20*20*3];
static uint8_t sprite_buf_asking[20*20*3];
static uint8_t sprite_buf_sleeping[20*20*3];
static uint8_t sprite_buf_dead[20*20*3];
static lv_image_dsc_t sprite_dsc_working;
static lv_image_dsc_t sprite_dsc_asking;
static lv_image_dsc_t sprite_dsc_sleeping;
static lv_image_dsc_t sprite_dsc_dead;

// Sessions screen layout geometry (shared by build + update for centering math)
static const int16_t SESSION_HEADER_H = 60;
static const int16_t SESSION_ROW_H    = 72;
static const int16_t SESSION_ROW_GAP  = 8;

// ---- Sessions screen widgets ----
static lv_obj_t* sessions_container = nullptr;
static lv_obj_t* sessions_header_label = nullptr;
static lv_obj_t* sessions_badge = nullptr;   // used by T10 splash badge; declared here
static lv_obj_t* sessions_empty_label = nullptr;
static lv_obj_t* session_rows[MAX_SESSIONS];
static lv_obj_t* session_row_imgs[MAX_SESSIONS];
static lv_obj_t* session_row_names[MAX_SESSIONS];
static lv_obj_t* session_row_pills[MAX_SESSIONS];   // pill TEXT label (child of badge)
static lv_obj_t* session_row_badges[MAX_SESSIONS];  // pill BADGE container (mood-tinted bg)
static lv_obj_t* session_row_details[MAX_SESSIONS]; // detail sub-text label per row
static lv_obj_t* sessions_title_label = nullptr;    // "Sessions" header title
static lv_obj_t* sessions_divider     = nullptr;    // 1px rule under header
static int8_t    ui_focused_idx = -1;
static char      ui_focused_id[9] = {};  // id of focused session; re-matched across payload refreshes
// Module-level pointer set by ui_update_sessions on every payload refresh.
// row_tap_cb (defined below) references it.
static const UsageData* s_latest_data = nullptr;

// ---- Shared ----
static lv_image_dsc_t logo_dsc;
static screen_t current_screen = SCREEN_USAGE;
static bool     s_ble_connected = false;   // cached BLE connection state
static uint32_t connected_at_ms = 0;       // when we last entered CONNECTED ("Connected" dwell)

// Animation state
static uint32_t anim_last_ms = 0;
static uint8_t anim_spinner_idx = 0;
static uint8_t anim_phase = 0;
static uint8_t anim_msg_idx = 0;
static uint32_t anim_msg_start = 0;
#define ANIM_MSG_MS     4000

static const char* const spinner_frames[] = {
    "\xC2\xB7", "\xE2\x9C\xBB", "\xE2\x9C\xBD",
    "\xE2\x9C\xB6", "\xE2\x9C\xB3", "\xE2\x9C\xA2",
};
#define SPINNER_COUNT 6
#define SPINNER_PHASES (2 * (SPINNER_COUNT - 1))  // 10: ping-pong 0..5..0

static const uint16_t spinner_ms[SPINNER_COUNT] = {
    260, 130, 130, 130, 130, 260,
};

static const char* const anim_messages[] = {
    "Accomplishing", "Elucidating", "Perusing",
    "Actioning", "Enchanting", "Philosophising",
    "Actualizing", "Envisioning", "Pondering",
    "Baking", "Finagling", "Pontificating",
    "Booping", "Flibbertigibbeting", "Processing",
    "Brewing", "Forging", "Puttering",
    "Calculating", "Forming", "Puzzling",
    "Cerebrating", "Frolicking", "Reticulating",
    "Channelling", "Generating", "Ruminating",
    "Churning", "Germinating", "Scheming",
    "Clauding", "Hatching", "Schlepping",
    "Coalescing", "Herding", "Shimmying",
    "Cogitating", "Honking", "Shucking",
    "Combobulating", "Hustling", "Simmering",
    "Computing", "Ideating", "Smooshing",
    "Concocting", "Imagining", "Spelunking",
    "Conjuring", "Incubating", "Spinning",
    "Considering", "Inferring", "Stewing",
    "Contemplating", "Jiving", "Sussing",
    "Cooking", "Manifesting", "Synthesizing",
    "Crafting", "Marinating", "Thinking",
    "Creating", "Meandering", "Tinkering",
    "Crunching", "Moseying", "Transmuting",
    "Deciphering", "Mulling", "Unfurling",
    "Deliberating", "Mustering", "Unravelling",
    "Determining", "Musing", "Vibing",
    "Discombobulating", "Noodling", "Wandering",
    "Divining", "Percolating", "Whirring",
    "Doing", "Wibbling",
    "Effecting", "Wizarding",
    "Working", "Wrangling",
};
#define ANIM_MSG_COUNT (sizeof(anim_messages) / sizeof(anim_messages[0]))

static lv_color_t pct_color(float pct) {
    if (pct >= 80.0f) return COL_RED;
    if (pct >= 50.0f) return COL_AMBER;
    return COL_GREEN;
}

static void format_reset_time(int mins, char* buf, size_t len) {
    if (mins < 0) {
        snprintf(buf, len, "---");
    } else if (mins < 60) {
        snprintf(buf, len, "Resets in %dm", mins);
    } else if (mins < 1440) {
        snprintf(buf, len, "Resets in %dh %dm", mins / 60, mins % 60);
    } else {
        snprintf(buf, len, "Resets in %dd %dh", mins / 1440, (mins % 1440) / 60);
    }
}

// Forward decls — callbacks defined near ui_show_screen below
static void global_click_cb(lv_event_t* e);

static lv_obj_t* make_panel(lv_obj_t* parent, int x, int y, int w, int h) {
    lv_obj_t* panel = lv_obj_create(parent);
    lv_obj_set_pos(panel, x, y);
    lv_obj_set_size(panel, w, h);
    lv_obj_set_style_bg_color(panel, COL_PANEL, 0);
    lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(panel, 8, 0);
    lv_obj_set_style_border_width(panel, 0, 0);
    lv_obj_set_style_pad_left(panel, 16, 0);
    lv_obj_set_style_pad_right(panel, 16, 0);
    lv_obj_set_style_pad_top(panel, 12, 0);
    lv_obj_set_style_pad_bottom(panel, 12, 0);
    lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(panel, LV_OBJ_FLAG_EVENT_BUBBLE);
    return panel;
}

static lv_obj_t* make_bar(lv_obj_t* parent, int x, int y, int w, int h) {
    lv_obj_t* bar = lv_bar_create(parent);
    lv_obj_set_pos(bar, x, y);
    lv_obj_set_size(bar, w, h);
    lv_bar_set_range(bar, 0, 100);
    lv_bar_set_value(bar, 0, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(bar, COL_BAR_BG, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_radius(bar, 6, LV_PART_MAIN);
    lv_obj_set_style_bg_color(bar, COL_GREEN, LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_radius(bar, 6, LV_PART_INDICATOR);
    return bar;
}

static void init_icon_dsc_rgb565a8(lv_image_dsc_t* dsc, int w, int h, const uint8_t* data) {
    dsc->header.w = w;
    dsc->header.h = h;
    dsc->header.cf = LV_COLOR_FORMAT_RGB565A8;
    dsc->header.stride = w * 2;
    dsc->data = data;
    dsc->data_size = w * h * 3;
}

static void build_mood_sprite(
    lv_image_dsc_t* dsc, uint8_t* buf,
    const splash_anim_def_t* anim, bool greyscale)
{
    const int N = 20 * 20;
    uint8_t* rgb_plane   = buf;
    uint8_t* alpha_plane = buf + N * 2;
    for (int i = 0; i < N; i++) {
        uint8_t idx = anim->frames[0][i];
        uint16_t pal_rgb565 = anim->palette[idx];
        if (greyscale) {
            uint8_t r5 = (pal_rgb565 >> 11) & 0x1F;
            uint8_t g6 = (pal_rgb565 >> 5)  & 0x3F;
            uint8_t b5 =  pal_rgb565        & 0x1F;
            uint8_t r8 = (r5 << 3) | (r5 >> 2);
            uint8_t g8 = (g6 << 2) | (g6 >> 4);
            uint8_t b8 = (b5 << 3) | (b5 >> 2);
            uint8_t luma = (uint8_t)(0.299f * r8 + 0.587f * g8 + 0.114f * b8);
            uint8_t l5 = luma >> 3;
            uint8_t l6 = (uint8_t)(l5 << 1);
            pal_rgb565 = (l5 << 11) | (l6 << 5) | l5;
        }
        // Little-endian RGB565: low byte first (matches icons.h icon data format)
        rgb_plane[i * 2]     = (uint8_t)(pal_rgb565 & 0xFF);
        rgb_plane[i * 2 + 1] = (uint8_t)(pal_rgb565 >> 8);
        alpha_plane[i] = (idx == 0) ? 0 : 255;
    }
    init_icon_dsc_rgb565a8(dsc, 20, 20, buf);
}

static void build_mood_sprites(void) {
    // splash_anims[] is defined in splash_animations.h with SPLASH_ANIM_COUNT entries.
    // Names use spaces (e.g. "work coding"), not underscores.
    const splash_anim_def_t* anim_work  = nullptr;
    const splash_anim_def_t* anim_ask   = nullptr;
    const splash_anim_def_t* anim_sleep = nullptr;
    for (int i = 0; i < SPLASH_ANIM_COUNT; i++) {
        const char* n = splash_anims[i].name;
        if (!anim_work  && strcmp(n, "work coding") == 0)         anim_work  = &splash_anims[i];
        if (!anim_ask   && strcmp(n, "expression surprise") == 0) anim_ask   = &splash_anims[i];
        if (!anim_sleep && strcmp(n, "expression sleep") == 0)    anim_sleep = &splash_anims[i];
    }
    int built = 0;
    if (anim_work)  { build_mood_sprite(&sprite_dsc_working,  sprite_buf_working,  anim_work,  false); built++; }
    else              Serial.printf("Sprites: WARNING 'work coding' not found\n");
    if (anim_ask)   { build_mood_sprite(&sprite_dsc_asking,   sprite_buf_asking,   anim_ask,   false); built++; }
    else              Serial.printf("Sprites: WARNING 'expression surprise' not found\n");
    if (anim_sleep) { build_mood_sprite(&sprite_dsc_sleeping, sprite_buf_sleeping, anim_sleep, false);
                      build_mood_sprite(&sprite_dsc_dead,     sprite_buf_dead,     anim_sleep, true); built += 2; }
    else              Serial.printf("Sprites: WARNING 'expression sleep' not found\n");
    Serial.printf("Sprites: built %d mood sprites (%d B)\n", built, built * 20 * 20 * 3);
    lv_mem_monitor_t mon;
    lv_mem_monitor(&mon);
    Serial.printf("Sprites: lv_mem free=%zu PSRAM free=%zu\n",
        (size_t)mon.free_size,
        heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}

static lv_obj_t* make_pill(lv_obj_t* parent, const char* text) {
    lv_obj_t* lbl = lv_label_create(parent);
    lv_label_set_text(lbl, text);
    lv_obj_set_style_text_font(lbl, &font_styrene_28, 0);
    lv_obj_set_style_text_color(lbl, COL_TEXT, 0);
    lv_obj_set_style_bg_color(lbl, COL_BAR_BG, 0);
    lv_obj_set_style_bg_opa(lbl, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(lbl, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_pad_left(lbl, 18, 0);
    lv_obj_set_style_pad_right(lbl, 18, 0);
    lv_obj_set_style_pad_top(lbl, 6, 0);
    lv_obj_set_style_pad_bottom(lbl, 6, 0);
    return lbl;
}

static void init_battery_icons(void) {
    init_icon_dsc_rgb565a8(&battery_dscs[0], ICON_BATTERY_W, ICON_BATTERY_H, icon_battery_data);
    init_icon_dsc_rgb565a8(&battery_dscs[1], ICON_BATTERY_LOW_W, ICON_BATTERY_LOW_H, icon_battery_low_data);
    init_icon_dsc_rgb565a8(&battery_dscs[2], ICON_BATTERY_MEDIUM_W, ICON_BATTERY_MEDIUM_H, icon_battery_medium_data);
    init_icon_dsc_rgb565a8(&battery_dscs[3], ICON_BATTERY_FULL_W, ICON_BATTERY_FULL_H, icon_battery_full_data);
    init_icon_dsc_rgb565a8(&battery_dscs[4], ICON_BATTERY_CHARGING_W, ICON_BATTERY_CHARGING_H, icon_battery_charging_data);
}

// ======== Usage Screen ========

static void make_usage_panel(lv_obj_t* parent, int y, const char* pill_text,
                             lv_obj_t** out_pct, lv_obj_t** out_pill,
                             lv_obj_t** out_bar, lv_obj_t** out_reset) {
    lv_obj_t* panel = make_panel(parent, L.margin, y, L.content_w, L.usage_panel_h);

    *out_pct = lv_label_create(panel);
    lv_label_set_text(*out_pct, "---%");
    lv_obj_set_style_text_font(*out_pct, &font_styrene_48, 0);
    lv_obj_set_style_text_color(*out_pct, COL_TEXT, 0);
    lv_obj_set_pos(*out_pct, 0, 0);

    *out_pill = make_pill(panel, pill_text);
    lv_obj_align(*out_pill, LV_ALIGN_TOP_RIGHT, 0, 1);

    *out_bar = make_bar(panel, 0, L.usage_bar_y, L.content_w - 32, 24);

    *out_reset = lv_label_create(panel);
    lv_label_set_text(*out_reset, "---");
    lv_obj_set_style_text_font(*out_reset, &font_styrene_28, 0);
    lv_obj_set_style_text_color(*out_reset, COL_DIM, 0);
    lv_obj_set_pos(*out_reset, 0, L.usage_reset_y);
}

// Pairing hint — shown when disconnected so the screen isn't empty and the
// user knows how to (re)pair. Wording matches the 3-second release gesture.
static void build_pair_group(lv_obj_t* parent) {
    pair_group = lv_obj_create(parent);
    lv_obj_set_size(pair_group, L.scr_w, L.scr_h - L.content_y);
    lv_obj_set_pos(pair_group, 0, L.content_y);
    lv_obj_set_style_bg_opa(pair_group, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(pair_group, 0, 0);
    lv_obj_set_style_pad_all(pair_group, 0, 0);
    lv_obj_clear_flag(pair_group, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(pair_group, LV_OBJ_FLAG_EVENT_BUBBLE);

    lv_obj_t* l1 = lv_label_create(pair_group);
    lv_label_set_text(l1, "To pair");
    lv_obj_set_style_text_font(l1, L.bt_status_font, 0);
    lv_obj_set_style_text_color(l1, COL_TEXT, 0);
    lv_obj_align(l1, LV_ALIGN_TOP_MID, 0, 40);

    lv_obj_t* l2 = lv_label_create(pair_group);
    lv_label_set_text(l2, "hold the power button");
    lv_obj_set_style_text_font(l2, L.bt_device_font, 0);
    lv_obj_set_style_text_color(l2, COL_DIM, 0);
    lv_obj_align(l2, LV_ALIGN_TOP_MID, 0, 120);

    lv_obj_t* l3 = lv_label_create(pair_group);
    lv_label_set_text(l3, "for 3 seconds, then release");
    lv_obj_set_style_text_font(l3, L.bt_device_font, 0);
    lv_obj_set_style_text_color(l3, COL_DIM, 0);
    lv_obj_align(l3, LV_ALIGN_TOP_MID, 0, 160);

    lv_obj_add_flag(pair_group, LV_OBJ_FLAG_HIDDEN);  // ui_update_ble_status decides
}

// Idle "Zzz" screen — shown when the host is connected but no usage update has
// landed recently (token expired, daemon down, host asleep…). Full-screen, like
// the pairing hint, so we never render hours-old numbers as if they were live.
static void build_idle_group(lv_obj_t* parent) {
    idle_group = lv_obj_create(parent);
    lv_obj_set_size(idle_group, L.scr_w, L.scr_h - L.content_y);
    lv_obj_set_pos(idle_group, 0, L.content_y);
    lv_obj_set_style_bg_opa(idle_group, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(idle_group, 0, 0);
    lv_obj_set_style_pad_all(idle_group, 0, 0);
    lv_obj_clear_flag(idle_group, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(idle_group, LV_OBJ_FLAG_EVENT_BUBBLE);

    // A shrunk-down sleeping creature (reused claudepix "expression sleep" art)
    // sits between the header and the status line; the animated "Listening…"
    // status line carries the words, so no extra text is needed here.
    lv_obj_t* creature = splash_mini_create(idle_group, "expression sleep", 160);
    if (creature) lv_obj_align(creature, LV_ALIGN_CENTER, 0, -20);

    lv_obj_add_flag(idle_group, LV_OBJ_FLAG_HIDDEN);  // update_view_state decides
}

static void init_usage_screen(lv_obj_t* scr) {
    usage_container = lv_obj_create(scr);
    lv_obj_set_size(usage_container, L.scr_w, L.scr_h);
    lv_obj_set_pos(usage_container, 0, 0);
    lv_obj_set_style_bg_opa(usage_container, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(usage_container, 0, 0);
    lv_obj_set_style_pad_all(usage_container, 0, 0);
    lv_obj_clear_flag(usage_container, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(usage_container, global_click_cb, LV_EVENT_CLICKED, NULL);

    lbl_title = lv_label_create(usage_container);
    lv_label_set_text(lbl_title, "Usage");
    lv_obj_set_style_text_font(lbl_title, &font_tiempos_56, 0);
    lv_obj_set_style_text_color(lbl_title, COL_TEXT, 0);
    lv_obj_align(lbl_title, LV_ALIGN_TOP_MID, 16, L.title_y);

    // Usage panels (shown when connected) live in a transparent full-size group
    // so they can be toggled against the pairing hint as one unit.
    usage_group = lv_obj_create(usage_container);
    lv_obj_set_size(usage_group, L.scr_w, L.scr_h);
    lv_obj_set_pos(usage_group, 0, 0);
    lv_obj_set_style_bg_opa(usage_group, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(usage_group, 0, 0);
    lv_obj_set_style_pad_all(usage_group, 0, 0);
    lv_obj_clear_flag(usage_group, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(usage_group, LV_OBJ_FLAG_EVENT_BUBBLE);

    make_usage_panel(usage_group, L.content_y, "Current",
                     &lbl_session_pct, &lbl_session_label,
                     &bar_session, &lbl_session_reset);
    make_usage_panel(usage_group,
                     L.content_y + L.usage_panel_h + L.usage_panel_gap, "Weekly",
                     &lbl_weekly_pct, &lbl_weekly_label,
                     &bar_weekly, &lbl_weekly_reset);

    build_pair_group(usage_container);
    build_idle_group(usage_container);

    // Status line — always visible on the usage view. Driven by ui_tick_anim().
    lbl_anim = lv_label_create(usage_container);
    lv_label_set_text(lbl_anim, "");
    lv_obj_set_style_text_font(lbl_anim, &font_mono_32, 0);
    lv_obj_set_style_text_color(lbl_anim, COL_ACCENT, 0);
    lv_obj_align(lbl_anim, LV_ALIGN_BOTTOM_MID, 0, -15);
}

// ---- Sessions screen helpers ----

static lv_timer_t* s_return_timer = nullptr;

static void row_tap_cb(lv_event_t* e) {
    int8_t idx = (int8_t)(intptr_t)lv_event_get_user_data(e);
    // Guard: tapping a row beyond valid range is a no-op
    if (!s_latest_data || idx < 0 || idx >= (int8_t)s_latest_data->session_count) return;
    ui_notify_focus_by_idx(s_latest_data, idx);
    for (int i = 0; i < MAX_SESSIONS; i++) {
        if (!session_rows[i]) continue;
        bool focused = (i == idx);
        lv_obj_set_style_bg_color(session_rows[i],
            focused ? lv_color_hex(0x1F1000) : lv_color_hex(0x141414), 0);
        lv_obj_set_style_border_width(session_rows[i], focused ? 1 : 0, 0);
        lv_obj_set_style_border_color(session_rows[i], lv_color_hex(0xF97316), 0);
        // FIX A: sync opacity on tap — focused idle/dead must not be dimmed
        bool dim = false;
        if (s_latest_data && i < (int)s_latest_data->session_count) {
            uint8_t m = s_latest_data->sessions[i].mood;
            dim = (m == MOOD_SLEEPING || m == MOOD_DEAD) && !focused;
        }
        lv_obj_set_style_opa(session_rows[i], dim ? (lv_opa_t)140 : LV_OPA_COVER, 0);
    }
    // Cancel any pending auto-return before arming a fresh one
    if (s_return_timer) { lv_timer_delete(s_return_timer); s_return_timer = nullptr; }
    s_return_timer = lv_timer_create([](lv_timer_t* t) {
        s_return_timer = nullptr;
        lv_timer_delete(t);
        if (current_screen == SCREEN_SESSIONS) ui_show_screen(SCREEN_USAGE);
    }, 600, nullptr);
}

static void build_sessions_container(lv_obj_t* scr) {
    // Header height: ~56px; rows area below is scrollable
    const int16_t header_h = SESSION_HEADER_H;
    const int16_t row_h    = SESSION_ROW_H;

    // Font selection: scale up on large (480×480) board, keep compact sizes on 1.8" board
    const bool large_board = (board_caps().height >= 460);
    const lv_font_t* sess_count_font  = large_board ? &font_styrene_24 : L.bt_credit_2_font;
    const lv_font_t* sess_name_font   = large_board ? &font_styrene_28 : L.bt_credit_1_font;
    const lv_font_t* sess_pill_font   = large_board ? &font_styrene_20 : &font_styrene_14;
    const lv_font_t* sess_detail_font = large_board ? &font_styrene_20 : &font_styrene_16;

    sessions_container = lv_obj_create(scr);
    lv_obj_set_size(sessions_container, L.scr_w, L.scr_h);
    lv_obj_set_pos(sessions_container, 0, 0);
    lv_obj_set_style_bg_color(sessions_container, COL_BG, 0);
    lv_obj_set_style_bg_opa(sessions_container, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(sessions_container, 0, 0);
    lv_obj_set_style_pad_all(sessions_container, 0, 0);
    lv_obj_clear_flag(sessions_container, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(sessions_container, LV_OBJ_FLAG_HIDDEN);  // hidden until shown

    // Header bar — this is the tap-cycle zone inside sessions screen
    lv_obj_t* header = lv_obj_create(sessions_container);
    lv_obj_set_size(header, L.scr_w, header_h);
    lv_obj_set_pos(header, 0, 0);
    lv_obj_set_style_bg_color(header, COL_BG, 0);
    lv_obj_set_style_bg_opa(header, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(header, 0, 0);
    lv_obj_set_style_pad_all(header, 0, 0);
    lv_obj_clear_flag(header, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(header, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(header, global_click_cb, LV_EVENT_CLICKED, NULL);

    // "Sessions" title — left-aligned, prominent (Change C)
    sessions_title_label = lv_label_create(header);
    lv_label_set_text(sessions_title_label, "Sessions");
    lv_obj_set_style_text_font(sessions_title_label, L.bt_device_font, 0);
    lv_obj_set_style_text_color(sessions_title_label, COL_TEXT, 0);
    lv_obj_align(sessions_title_label, LV_ALIGN_LEFT_MID, L.margin, 0);

    // Count label — right-aligned, dim (Change C/F)
    sessions_header_label = lv_label_create(header);
    lv_label_set_text(sessions_header_label, "0 / 0% of 5h");
    lv_obj_set_style_text_font(sessions_header_label, sess_count_font, 0);
    lv_obj_set_style_text_color(sessions_header_label, COL_DIM, 0);
    lv_obj_align(sessions_header_label, LV_ALIGN_RIGHT_MID, -L.margin, 0);

    // 1px divider rule at bottom of header (Change C)
    sessions_divider = lv_obj_create(sessions_container);
    lv_obj_set_size(sessions_divider, L.scr_w, 1);
    lv_obj_set_pos(sessions_divider, 0, header_h - 1);
    lv_obj_set_style_bg_color(sessions_divider, lv_color_hex(0x222222), 0);
    lv_obj_set_style_bg_opa(sessions_divider, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(sessions_divider, 0, 0);
    lv_obj_set_style_radius(sessions_divider, 0, 0);
    lv_obj_clear_flag(sessions_divider, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_clear_flag(sessions_divider, LV_OBJ_FLAG_CLICKABLE);

    // Rows area — scrollable flex column below header (Fix 1)
    lv_obj_t* rows_area = lv_obj_create(sessions_container);
    lv_obj_set_size(rows_area, L.scr_w, L.scr_h - header_h);
    lv_obj_set_pos(rows_area, 0, header_h);
    lv_obj_set_style_bg_opa(rows_area, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(rows_area, 0, 0);
    lv_obj_set_style_pad_all(rows_area, 0, 0);
    lv_obj_set_style_pad_top(rows_area, 6, 0);
    lv_obj_set_style_pad_bottom(rows_area, 6, 0);
    lv_obj_set_style_pad_left(rows_area, L.margin, 0);
    lv_obj_set_style_pad_right(rows_area, L.margin + 8, 0);  // extra room so cards don't overlap scrollbar
    lv_obj_set_style_pad_row(rows_area, SESSION_ROW_GAP, 0);
    lv_obj_set_flex_flow(rows_area, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(rows_area, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START);
    lv_obj_add_flag(rows_area, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_clear_flag(rows_area, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_scrollbar_mode(rows_area, LV_SCROLLBAR_MODE_AUTO);
    lv_obj_set_style_width(rows_area, 5, LV_PART_SCROLLBAR);
    lv_obj_set_style_radius(rows_area, 3, LV_PART_SCROLLBAR);
    lv_obj_set_style_bg_color(rows_area, lv_color_hex(0x666666), LV_PART_SCROLLBAR);
    lv_obj_set_style_bg_opa(rows_area, LV_OPA_50, LV_PART_SCROLLBAR);
    lv_obj_set_style_pad_right(rows_area, 3, LV_PART_SCROLLBAR);

    // Empty state label
    sessions_empty_label = lv_label_create(rows_area);
    lv_label_set_text(sessions_empty_label, "No sessions");
    lv_obj_set_style_text_color(sessions_empty_label, COL_DIM, 0);
    lv_obj_set_style_text_font(sessions_empty_label, L.bt_device_font, 0);
    lv_obj_align(sessions_empty_label, LV_ALIGN_CENTER, 0, 0);
    lv_obj_add_flag(sessions_empty_label, (lv_obj_flag_t)(LV_OBJ_FLAG_HIDDEN | LV_OBJ_FLAG_IGNORE_LAYOUT));

    // Build MAX_SESSIONS row slots (Change D — two-line card layout)
    const int16_t row_inner_w = L.scr_w - L.margin * 2;
    for (int i = 0; i < MAX_SESSIONS; i++) {
        lv_obj_t* row = lv_obj_create(rows_area);
        lv_obj_set_width(row, lv_pct(100));
        lv_obj_set_flex_grow(row, 1);
        lv_obj_set_style_min_height(row, 72, 0);
        lv_obj_set_style_bg_color(row, lv_color_hex(0x141414), 0);
        lv_obj_set_style_bg_opa(row, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(row, 0, 0);
        lv_obj_set_style_radius(row, 8, 0);
        lv_obj_set_style_pad_all(row, 0, 0);
        lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(row, LV_OBJ_FLAG_CLICKABLE);
        // NO LV_OBJ_FLAG_EVENT_BUBBLE — taps must NOT cycle the screen
        lv_obj_add_event_cb(row, row_tap_cb, LV_EVENT_CLICKED, (void*)(intptr_t)i);
        lv_obj_add_flag(row, (lv_obj_flag_t)(LV_OBJ_FLAG_HIDDEN | LV_OBJ_FLAG_IGNORE_LAYOUT));
        session_rows[i] = row;

        // Sprite image (20x20, scaled 2x = 40px, left-anchored, vertically centered)
        lv_obj_t* img = lv_image_create(row);
        lv_obj_align(img, LV_ALIGN_LEFT_MID, 10, 0);
        lv_image_set_scale(img, 512);
        session_row_imgs[i] = img;

        // INFO column — vertical flex, sprite_w=40 + left_pad=10 + gap=10 = x=60
        const int16_t info_x = 60;
        const int16_t info_w = row_inner_w - info_x - 8;
        lv_obj_t* info = lv_obj_create(row);
        lv_obj_set_size(info, info_w, lv_pct(100));
        lv_obj_set_pos(info, info_x, 0);
        lv_obj_set_style_bg_opa(info, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(info, 0, 0);
        lv_obj_set_style_pad_all(info, 0, 0);
        lv_obj_set_style_pad_row(info, 2, 0);
        lv_obj_clear_flag(info, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_clear_flag(info, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_flex_flow(info, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_flex_align(info, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);

        // Line 1: session NAME
        lv_obj_t* name_lbl = lv_label_create(info);
        lv_obj_set_width(name_lbl, info_w);
        lv_obj_set_style_text_font(name_lbl, sess_name_font, 0);
        lv_obj_set_style_text_color(name_lbl, COL_TEXT, 0);
        lv_label_set_long_mode(name_lbl, LV_LABEL_LONG_DOT);
        lv_label_set_text(name_lbl, "");
        session_row_names[i] = name_lbl;

        // Line 2: META row — horizontal flex (badge + detail)
        lv_obj_t* meta = lv_obj_create(info);
        lv_obj_set_size(meta, info_w, LV_SIZE_CONTENT);
        lv_obj_set_style_bg_opa(meta, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(meta, 0, 0);
        lv_obj_set_style_pad_all(meta, 0, 0);
        lv_obj_set_style_pad_column(meta, 6, 0);
        lv_obj_clear_flag(meta, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_clear_flag(meta, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_flex_flow(meta, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(meta, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);

        // Badge container (mood-tinted rounded rect)
        lv_obj_t* badge = lv_obj_create(meta);
        lv_obj_set_size(badge, LV_SIZE_CONTENT, LV_SIZE_CONTENT);  // auto-fit pill font (pad_ver gives breathing room)
        lv_obj_set_style_bg_color(badge, lv_color_hex(0x6B7280), 0);  // default grey; overridden in update
        lv_obj_set_style_bg_opa(badge, (lv_opa_t)(255 * 70 / 100), 0);
        lv_obj_set_style_border_width(badge, 0, 0);
        lv_obj_set_style_radius(badge, 8, 0);
        lv_obj_set_style_pad_ver(badge, 2, 0);
        lv_obj_set_style_pad_hor(badge, 8, 0);
        lv_obj_clear_flag(badge, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_clear_flag(badge, LV_OBJ_FLAG_CLICKABLE);
        session_row_badges[i] = badge;

        // Pill text label (child of badge)
        lv_obj_t* pill = lv_label_create(badge);
        lv_obj_set_style_text_font(pill, sess_pill_font, 0);
        lv_obj_set_style_text_color(pill, lv_color_hex(0x9CA3AF), 0);  // default; overridden in update
        lv_label_set_long_mode(pill, LV_LABEL_LONG_CLIP);
        lv_label_set_text(pill, "IDLE");
        lv_obj_align(pill, LV_ALIGN_CENTER, 0, 0);
        session_row_pills[i] = pill;

        // Detail sub-text (flex-grow to fill remaining width)
        lv_obj_t* detail = lv_label_create(meta);
        lv_obj_set_style_text_font(detail, sess_detail_font, 0);
        lv_obj_set_style_text_color(detail, COL_DIM, 0);
        lv_label_set_long_mode(detail, LV_LABEL_LONG_DOT);
        lv_label_set_text(detail, "");
        lv_obj_set_flex_grow(detail, 1);
        session_row_details[i] = detail;
    }
}

// ======== Public API ========

void ui_init(void) {
    compute_layout(board_caps());

    build_mood_sprites();

    lv_obj_t* scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, COL_BG, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    init_icon_dsc_rgb565a8(&logo_dsc, LOGO_WIDTH, LOGO_HEIGHT, logo_data);
    init_battery_icons();

    init_usage_screen(scr);
    splash_init(scr);
    build_sessions_container(scr);

    if (splash_get_root()) {
        lv_obj_add_event_cb(splash_get_root(), global_click_cb, LV_EVENT_CLICKED, NULL);
    }

    logo_img = lv_image_create(scr);
    lv_image_set_src(logo_img, &logo_dsc);
    lv_obj_set_pos(logo_img, L.margin, L.title_y - 10);

    battery_img = lv_image_create(scr);
    lv_image_set_src(battery_img, &battery_dscs[0]);
    lv_obj_set_pos(battery_img, L.scr_w - 48 - L.margin, L.title_y);

}

void ui_update(const UsageData* data) {
    if (!data->valid) return;
    last_data_ms = lv_tick_get();   // a valid usage update just landed → dot goes green
    data_received = true;

    int s_pct = (int)(data->session_pct + 0.5f);

    lv_label_set_text_fmt(lbl_session_pct, "%d%%", s_pct);
    lv_bar_set_value(bar_session, s_pct, LV_ANIM_ON);
    lv_obj_set_style_bg_color(bar_session, pct_color(data->session_pct), LV_PART_INDICATOR);

    char buf[48];
    format_reset_time(data->session_reset_mins, buf, sizeof(buf));
    lv_label_set_text(lbl_session_reset, buf);

    int w_pct = (int)(data->weekly_pct + 0.5f);
    lv_label_set_text_fmt(lbl_weekly_pct, "%d%%", w_pct);
    lv_bar_set_value(bar_weekly, w_pct, LV_ANIM_ON);
    lv_obj_set_style_bg_color(bar_weekly, pct_color(data->weekly_pct), LV_PART_INDICATOR);

    format_reset_time(data->weekly_reset_mins, buf, sizeof(buf));
    lv_label_set_text(lbl_weekly_reset, buf);
}

// Pick the usage-view sub-screen: pairing hint (BLE down), the idle "Zzz" screen
// (connected but data has gone stale), or the live usage panels. Only re-lays-out
// on an actual change. The animated status line stays visible everywhere — it
// reads "Listening…" on the idle screen, keeping it alive rather than frozen.
static void update_view_state(void) {
    if (!usage_group || !pair_group || !idle_group) return;
    int v;
    if (!s_ble_connected) {
        v = 0;  // pairing hint
    } else if (data_received && (lv_tick_get() - last_data_ms) < DATA_FRESH_MS) {
        v = 2;  // live usage
    } else {
        v = 1;  // idle / Zzz
    }
    if (v == view_state) return;
    view_state = v;
    lv_obj_add_flag(pair_group, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(idle_group, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(usage_group, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(v == 0 ? pair_group : v == 1 ? idle_group : usage_group,
                      LV_OBJ_FLAG_HIDDEN);
}

void ui_tick_anim(void) {
    if (current_screen != SCREEN_USAGE) return;
    update_view_state();
    if (view_state == 1) splash_mini_tick();   // animate the sleeping creature on the idle screen

    uint32_t now = lv_tick_get();

    if (now - anim_msg_start >= ANIM_MSG_MS) {
        anim_msg_idx = (anim_msg_idx + 1) % ANIM_MSG_COUNT;
        anim_msg_start = now;
    }

    if (now - anim_last_ms < spinner_ms[anim_spinner_idx]) return;
    anim_last_ms = now;
    anim_phase = (anim_phase + 1) % SPINNER_PHASES;
    anim_spinner_idx = (anim_phase < SPINNER_COUNT) ? anim_phase
                                                    : (SPINNER_PHASES - anim_phase);

    // Status text by priority. Whimsical messages only when connected & settled.
    const char* text;
    if (!s_ble_connected) {
        text = "Waiting";              // advertising / waiting for a host connection
    } else if (view_state == 1) {      // idle — alternate so it reads as alive AND data-less
        text = (anim_msg_idx & 1) ? "No data" : "Listening";
    } else if (now - connected_at_ms < 5000) {
        text = "Connected";
    } else {
        text = anim_messages[anim_msg_idx];
    }

    // All states share the whimsical style: "<glyph> <Title-case word>…"
    static char buf[80];
    snprintf(buf, sizeof(buf), "%s %s\xE2\x80\xA6",
             spinner_frames[anim_spinner_idx], text);
    lv_label_set_text(lbl_anim, buf);
}

static screen_t prev_non_splash_screen = SCREEN_USAGE;
static void apply_battery_visibility(void) {
    if (!battery_img) return;
    if (current_screen == SCREEN_SESSIONS) lv_obj_add_flag(battery_img, LV_OBJ_FLAG_HIDDEN);
    else                                   lv_obj_clear_flag(battery_img, LV_OBJ_FLAG_HIDDEN);
}

static void global_click_cb(lv_event_t* e) {
    (void)e;
    screen_t cur = current_screen;
    screen_t next;
    switch (cur) {
        case SCREEN_SPLASH:   next = SCREEN_USAGE;    break;
        case SCREEN_USAGE:    next = SCREEN_SESSIONS; break;
        case SCREEN_SESSIONS: next = SCREEN_SPLASH;   break;
        default:              next = SCREEN_USAGE;    break;
    }
    ui_show_screen(next);
}

void ui_show_screen(screen_t screen) {
    lv_obj_add_flag(usage_container, LV_OBJ_FLAG_HIDDEN);
    splash_hide();
    if (sessions_container) lv_obj_add_flag(sessions_container, LV_OBJ_FLAG_HIDDEN);

    switch (screen) {
    case SCREEN_SPLASH:
        splash_show();
        break;
    case SCREEN_USAGE:
        lv_obj_clear_flag(usage_container, LV_OBJ_FLAG_HIDDEN);
        break;
    case SCREEN_SESSIONS:
        if (sessions_container) lv_obj_clear_flag(sessions_container, LV_OBJ_FLAG_HIDDEN);
        break;
    default:
        break;
    }

    if (logo_img) {
        if (screen == SCREEN_SPLASH || screen == SCREEN_SESSIONS)
            lv_obj_add_flag(logo_img, LV_OBJ_FLAG_HIDDEN);
        else
            lv_obj_clear_flag(logo_img, LV_OBJ_FLAG_HIDDEN);
    }

    if (screen != SCREEN_SPLASH) prev_non_splash_screen = screen;
    current_screen = screen;
    apply_battery_visibility();
}

void ui_toggle_splash(void) {
    if (current_screen == SCREEN_SPLASH) ui_show_screen(prev_non_splash_screen);
    else                                  ui_show_screen(SCREEN_SPLASH);
}

screen_t ui_get_current_screen(void) {
    return current_screen;
}

void ui_update_ble_status(ble_state_t state, const char* name, const char* mac) {
    (void)name; (void)mac;
    bool was_connected = s_ble_connected;
    s_ble_connected = (state == BLE_STATE_CONNECTED);

    if (s_ble_connected && !was_connected) connected_at_ms = lv_tick_get();
    // pair / idle / usage — picked from connection + data freshness.
    update_view_state();
}

void ui_update_battery(int percent, bool charging) {
    int idx;
    if (charging) {
        idx = 4;
    } else if (percent < 0) {
        idx = 0;
    } else if (percent <= 10) {
        idx = 0;
    } else if (percent <= 35) {
        idx = 1;
    } else if (percent <= 75) {
        idx = 2;
    } else {
        idx = 3;
    }
    lv_image_set_src(battery_img, &battery_dscs[idx]);
    apply_battery_visibility();
}

void ui_notify_focus_by_idx(const UsageData* data, int8_t idx) {
    ui_focused_idx = idx;
    if (data && idx >= 0 && idx < (int8_t)data->session_count) {
        strlcpy(ui_focused_id, data->sessions[idx].id, sizeof(ui_focused_id));
        ble_notify_focus(ui_focused_id);
    } else {
        ui_focused_id[0] = '\0';
        ble_notify_focus(nullptr);
    }
}

bool ui_has_focused_session(void) {
    return ui_focused_id[0] != '\0';
}

// Forward declaration — definition follows below (after session_mood_priority).
// Required because ui_update_sessions calls this function.
static void update_splash_mood(const UsageData* data);

void ui_update_sessions(const UsageData* data) {
    s_latest_data = data;

    if (!sessions_header_label) return;

    // Header count: "N / X% of 5h" — middot (U+00B7) not in styrene_20, use " / " (ASCII)
    char hdr[32];
    snprintf(hdr, sizeof(hdr), "%d / %d%% of 5h",
             data->session_count, (int)(data->session_pct + 0.5f));
    lv_label_set_text(sessions_header_label, hdr);

    // Empty state
    if (sessions_empty_label) {
        if (data->session_count == 0) {
            lv_obj_clear_flag(sessions_empty_label, LV_OBJ_FLAG_HIDDEN);
            lv_obj_align(sessions_empty_label, LV_ALIGN_CENTER, 0, 0);
        } else {
            lv_obj_add_flag(sessions_empty_label, LV_OBJ_FLAG_HIDDEN);
        }
    }

    // Focus re-match: if ui_focused_id set, find it in new payload
    if (ui_focused_id[0] != '\0') {
        int8_t found = -1;
        for (int i = 0; i < (int)data->session_count; i++) {
            if (strncmp(data->sessions[i].id, ui_focused_id, 8) == 0) {
                found = (int8_t)i;
                break;
            }
        }
        if (found < 0) {
            // Session vanished — clear focus
            ui_focused_idx = -1;
            ui_focused_id[0] = '\0';
            ble_notify_focus(nullptr);
        } else {
            ui_focused_idx = found;
        }
    } else {
        // No tap-selection active: follow the daemon-focused session (mockup parity)
        ui_focused_idx = (data->focused_idx >= 0 && data->focused_idx < (int)data->session_count)
                         ? data->focused_idx : -1;
    }

    // Update row widgets — flex layout handles vertical distribution
    for (int i = 0; i < MAX_SESSIONS; i++) {
        if (!session_rows[i]) continue;
        if (i < (int)data->session_count) {
            const SessionInfo& si = data->sessions[i];
            lv_obj_clear_flag(session_rows[i], LV_OBJ_FLAG_HIDDEN);
            lv_obj_clear_flag(session_rows[i], LV_OBJ_FLAG_IGNORE_LAYOUT);

            // Sprite src — guard: only set if dsc data non-null
            lv_image_dsc_t* dsc = nullptr;
            switch (si.mood) {
                case MOOD_WORKING:  dsc = &sprite_dsc_working;  break;
                case MOOD_ASKING:   dsc = &sprite_dsc_asking;   break;
                case MOOD_SLEEPING: dsc = &sprite_dsc_sleeping; break;
                case MOOD_DEAD:     dsc = &sprite_dsc_dead;     break;
                default:            break;
            }
            if (session_row_imgs[i]) {
                if (dsc && dsc->data != nullptr)
                    lv_image_set_src(session_row_imgs[i], dsc);
            }

            // Name
            if (session_row_names[i])
                lv_label_set_text(session_row_names[i], si.name);

            // Focus state — computed before opacity so focused rows are never dimmed
            bool focused = (i == (int)ui_focused_idx);

            // Pill badge: uppercase text + badge bg tint by mood
            {
                const char* pill_text   = "IDLE";
                uint32_t    pill_color  = 0x9CA3AF;  // pill text color
                uint32_t    badge_color = 0x6B7280;  // badge bg tint
                bool        dim_row     = false;
                switch (si.mood) {
                    case MOOD_WORKING:  pill_text = "RUNNING"; pill_color = 0x22C55E; badge_color = 0x22C55E; break;
                    case MOOD_ASKING:   pill_text = "WAITING"; pill_color = 0xF59E0B; badge_color = 0xF59E0B; break;
                    case MOOD_SLEEPING: pill_text = "IDLE";    pill_color = 0x9CA3AF; badge_color = 0x6B7280; dim_row = true; break;
                    case MOOD_DEAD:     pill_text = "ENDED";   pill_color = 0x9CA3AF; badge_color = 0x4B5563; dim_row = true; break;
                    default: break;
                }
                if (session_row_pills[i]) {
                    lv_label_set_text(session_row_pills[i], pill_text);
                    lv_obj_set_style_text_color(session_row_pills[i], lv_color_hex(pill_color), 0);
                }
                if (session_row_badges[i]) {
                    lv_obj_set_style_bg_color(session_row_badges[i], lv_color_hex(badge_color), 0);
                }
                if (session_row_details[i]) {
                    lv_label_set_text(session_row_details[i], si.detail);
                }
                // Idle/dead rows dimmed to ~55% opacity — except focused row
                lv_obj_set_style_opa(session_rows[i],
                    (dim_row && !focused) ? (lv_opa_t)140 : LV_OPA_COVER, 0);
            }

            // Focus highlight (orange border + tint for focused row)
            lv_obj_set_style_bg_color(session_rows[i],
                focused ? lv_color_hex(0x1F1000) : lv_color_hex(0x141414), 0);
            lv_obj_set_style_border_width(session_rows[i], focused ? 1 : 0, 0);
            lv_obj_set_style_border_color(session_rows[i], lv_color_hex(0xF97316), 0);
        } else {
            lv_obj_add_flag(session_rows[i], (lv_obj_flag_t)(LV_OBJ_FLAG_HIDDEN | LV_OBJ_FLAG_IGNORE_LAYOUT));
        }
    }
    update_splash_mood(data);
}

static int session_mood_priority(uint8_t mood) {
    switch (mood) {
        case MOOD_ASKING:   return 0;
        case MOOD_DEAD:     return 1;
        // Priority 2 is reserved for the hot overlay (session_pct >= 85) injected by update_splash_mood.
        case MOOD_WORKING:  return 3;
        case MOOD_SLEEPING: return 4;
        default:            return 5;
    }
}

static void update_splash_mood(const UsageData* data) {
    if (data->session_count == 0) {
        if (sessions_badge) lv_obj_add_flag(sessions_badge, LV_OBJ_FLAG_HIDDEN);
        splash_release_mood_hold();
        return;
    }
    int best_prio = 99;
    int best_idx = -1;
    for (int i = 0; i < data->session_count; i++) {
        int p = session_mood_priority(data->sessions[i].mood);
        if (p < best_prio) { best_prio = p; best_idx = i; }
    }
    bool hot = (data->session_pct >= 85.0f);
    if (hot && best_prio > 2) { best_prio = 2; best_idx = -1; }

    // Animation names match splash_anims[] — spaces, no underscores
    const char* anim_name = nullptr;
    if      (best_prio == 0) anim_name = "expression surprise";
    else if (best_prio == 1) anim_name = "expression sleep";
    else if (best_prio == 2) anim_name = "dance bounce dj";
    else if (best_prio == 3) anim_name = "work coding";
    else                     anim_name = "expression sleep";

    if (anim_name) splash_set_animation_by_name(anim_name);

    // Lazy-build badge on first call
    if (!sessions_badge) {
        lv_obj_t* splash_root = splash_get_root();
        sessions_badge = lv_label_create(splash_root);
        lv_obj_set_style_text_font(sessions_badge, &font_styrene_28, 0);
        lv_obj_set_style_text_color(sessions_badge, lv_color_hex(0xF59E0B), 0);
        lv_obj_set_style_bg_color(sessions_badge, lv_color_hex(0x261A00), 0);
        lv_obj_set_style_bg_opa(sessions_badge, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(sessions_badge, 14, 0);
        lv_obj_set_style_pad_hor(sessions_badge, 12, 0);
        lv_obj_set_style_pad_ver(sessions_badge, 4, 0);
        lv_obj_align(sessions_badge, LV_ALIGN_BOTTOM_MID, 0, -54);
    }

    if (best_idx >= 0) {
        char badge_buf[32];
        const char* suffix = (best_prio == 0) ? " needs you" : (best_prio == 1) ? " ended" : "";
        snprintf(badge_buf, sizeof(badge_buf), "%s%s",
            data->sessions[best_idx].name, suffix);
        lv_label_set_text(sessions_badge, badge_buf);
        lv_obj_clear_flag(sessions_badge, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(sessions_badge, LV_OBJ_FLAG_HIDDEN);
    }
}
