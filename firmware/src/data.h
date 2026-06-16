#pragma once
#include <Arduino.h>

#define MAX_SESSIONS 8
#define MOOD_WORKING  'w'
#define MOOD_ASKING   'a'
#define MOOD_SLEEPING 's'
#define MOOD_DEAD     'd'

struct SessionInfo {
    char id[9];      // 8-char prefix + NUL
    char name[17];   // ≤16 chars + NUL
    char detail[17]; // ≤16 chars + NUL
    uint8_t mood;    // MOOD_* constant
    bool focused;
};

struct UsageData {
    float session_pct;       // 5-hour window utilization (0-100)
    int session_reset_mins;  // minutes until session resets
    float weekly_pct;        // 7-day window utilization (0-100)
    int weekly_reset_mins;   // minutes until weekly resets
    char status[16];         // "allowed" or "limited"
    bool ok;                 // data parse succeeded
    bool valid;              // false until first successful parse
    SessionInfo sessions[MAX_SESSIONS];
    uint8_t session_count;
    int8_t focused_idx;   // -1 if none
};
