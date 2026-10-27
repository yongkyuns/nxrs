/* Owned ordinary regex controls for the actual static PCRE2 selected by nginx. */
#define PCRE2_CODE_UNIT_WIDTH 8
#include <pcre2.h>
#include <stdio.h>
#include <string.h>
int main(void) {
    char version[128] = {0};
    unsigned int jit = 99;
    if (pcre2_config(PCRE2_CONFIG_VERSION, version) <= 0 || strncmp(version, "10.49 ", 6) != 0 ||
        pcre2_config(PCRE2_CONFIG_JIT, &jit) != 0 || jit != 0) return 1;
    const char *pattern = "^/v1/cameras/[A-Za-z0-9_-]{1,64}/playback-grants$";
    int error; PCRE2_SIZE offset;
    pcre2_code *code = pcre2_compile((PCRE2_SPTR) pattern, PCRE2_ZERO_TERMINATED, 0, &error, &offset, NULL);
    if (!code) return 2;
    pcre2_match_data *data = pcre2_match_data_create_from_pattern(code, NULL);
    if (!data) { pcre2_code_free(code); return 3; }
    const char *accepted = "/v1/cameras/cam_reference/playback-grants";
    const char *refused[] = {"/v1/cameras//playback-grants", "/v1/cameras/cam.x/playback-grants", "/v1/cameras/cam_reference/playback-grants?token=x"};
    int result = pcre2_match(code, (PCRE2_SPTR) accepted, strlen(accepted), 0, 0, data, NULL) >= 0 ? 0 : 4;
    for (unsigned int i = 0; i < sizeof(refused)/sizeof(refused[0]); i++)
        if (pcre2_match(code, (PCRE2_SPTR) refused[i], strlen(refused[i]), 0, 0, data, NULL) != PCRE2_ERROR_NOMATCH) result = 5;
    pcre2_match_data_free(data); pcre2_code_free(code);
    if (result == 0) puts("PCRE2 10.49 static probe: JIT disabled, route acceptance and refusals pass");
    return result;
}
