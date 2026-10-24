"""Hash-locked mechanical taps for a named enqueue-owned diagnostic case.

No vendor calculation, threshold, cadence or decision rule is replaced. The
trace case supplies copies made on the preprocessing owner at enqueue, instead
of inspecting a ring that can be overwritten while a queued window is pending.
"""

import hashlib

RADAR_SHA256 = "4d414bf23abbb53574fd6f753e83a371a8ac668a377d8af6748e322be4fe3715"


def radar(source):
    if hashlib.sha256(source).hexdigest() != RADAR_SHA256:
        raise ValueError("trace hooks require exact pinned radar source")
    text = source.decode()
    edits = [
        (
            '#include "esp_radar_motion_dec.h"',
            '#include "esp_radar_motion_dec.h"\n#include "../../../main/trace.h"',
        ),
        (
            "} csi_data_buff_index_t;",
            "    uint32_t trace_token; /* Test-only queue identity, outside the index union. */\n} csi_data_buff_index_t;",
        ),
        (
            "    float compensate_gain = 0;",
            "    presence_trace_rx_sdk(filtered_info);\n    float compensate_gain = 0;",
        ),
        (
            "        csi_write_frame_to_ring(peer, ltf_data, subcarrier_len, filtered_info);",
            """        csi_write_frame_to_ring(peer, ltf_data, subcarrier_len, filtered_info);
        presence_trace_row_sdk((uintptr_t)peer, peer->window_ctx.next_seq % peer->window_ctx.buff_size,
                               filtered_info, ltf_data, ltf_len,
                               peer->csi_data_buff->amplitude[peer->window_ctx.next_seq % peer->window_ctx.buff_size],
                               subcarrier_len, s_ctx.dec_config.sub_carrier_step_size,
                               s_ctx.dec_config.amplitude_log_enabled);""",
        ),
        (
            "        if (csi_window_update(peer, &buff_index) == ESP_OK) {",
            """        if (csi_window_update(peer, &buff_index) == ESP_OK) {
            if (presence_trace_active()) {
                buff_index.trace_token = presence_trace_enqueue((uintptr_t)peer, buff_index.begin,
                    buff_index.window, peer->window_ctx.buff_size, subcarrier_len,
                    peer->csi_data_buff->seq_id, peer->csi_data_buff->timestamp);
            }""",
        ),
        (
            "    return esp_radar_motion_dec_compute(cols, csi_data_0_len, csi_data_0, csi_data_1_len, csi_data_1, motion_dec_buff_current);",
            """    if (presence_trace_active()) {
        return presence_trace_compute(buff_index->trace_token, cols, csi_data_0_len, csi_data_1_len, motion_dec_buff_current);
    }
    return esp_radar_motion_dec_compute(cols, csi_data_0_len, csi_data_0, csi_data_1_len, csi_data_1, motion_dec_buff_current);""",
        ),
        (
            "            if (peer->cal->calibrate_status == RADAR_CALIBRATE_PROGRESS && peer->motion_dec_buff_num >= 2) {",
            """            if (presence_trace_active()) presence_trace_basis(buff_index.trace_token, peer->waveform_wander_last);
            if (peer->cal->calibrate_status == RADAR_CALIBRATE_PROGRESS && peer->motion_dec_buff_num >= 2) {""",
        ),
        (
            "        int32_t time_spent  = peer->csi_data_buff->timestamp[buff_index.end]",
            """        presence_trace_decision_sdk(buff_index.trace_token, peer, &radar_info);
        int32_t time_spent  = peer->csi_data_buff->timestamp[buff_index.end]""",
        ),
    ]
    for before, after in edits:
        if text.count(before) != 1:
            raise ValueError("ambiguous trace insertion")
        text = text.replace(before, after)
    return text.encode()


def require_compensation_observer(disassembly):
    """Fail the build if the real RX path bypasses the corrected entry wrapper."""
    import re

    if not re.search(
        r"\bcall8\s+[0-9a-f]+ <__wrap_esp_csi_gain_ctrl_compensate_rx_gain>",
        disassembly,
    ):
        raise ValueError("RX path does not call the actual compensation observer")
    return {
        "rx_calls_compensation_wrapper": True,
        "rx_disassembly_sha256": hashlib.sha256(disassembly.encode()).hexdigest(),
    }
