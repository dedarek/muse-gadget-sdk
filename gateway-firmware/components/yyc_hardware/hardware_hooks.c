#include "muse_board.h"
#include "muse_settings.h"

const muse_board_t *muse_board;
bool muse_state_asleep(void) { return false; }
int muse_settings_volume(void) { return 30; }
int muse_settings_mic_gain(void) { return 24; }
bool muse_settings_speaker_on(void) { return true; }
