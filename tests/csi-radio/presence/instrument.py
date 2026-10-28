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
        ("    /* Select peer to dispatch this packet */",
         "    if (!presence_source_admit(info->mac)) return;\n    /* Select peer to dispatch this packet */"),
        (
            '#include "esp_radar_motion_dec.h"',
            '#include "esp_radar_motion_dec.h"\n#include "../../../main/trace.h"',
        ),
        (
            "} csi_data_buff_index_t;",
            "    uint32_t trace_token; /* Test-only queue identity, outside the index union. */\n    uint32_t failure_token;\n} csi_data_buff_index_t;",
        ),
        (
            "    float compensate_gain = 0;",
            "    presence_trace_rx_sdk(peer->mac, filtered_info);\n    float compensate_gain = 0;",
        ),
        (
            "        csi_write_frame_to_ring(peer, ltf_data, subcarrier_len, filtered_info);",
            """        csi_write_frame_to_ring(peer, ltf_data, subcarrier_len, filtered_info);
        presence_trace_row_sdk((uintptr_t)peer, peer->mac, peer->window_ctx.next_seq % peer->window_ctx.buff_size,
                               filtered_info, ltf_data, ltf_len,
                               peer->csi_data_buff->amplitude[peer->window_ctx.next_seq % peer->window_ctx.buff_size],
                               subcarrier_len, s_ctx.dec_config.sub_carrier_step_size,
                               s_ctx.dec_config.amplitude_log_enabled);""",
        ),
        (
            "        if (csi_window_update(peer, &buff_index) == ESP_OK) {",
            """        if (csi_window_update(peer, &buff_index) == ESP_OK) {
            if (presence_trace_active() && presence_trace_matches(peer->mac)) {
                buff_index.trace_token = presence_trace_enqueue((uintptr_t)peer, buff_index.begin,
                    buff_index.window, peer->window_ctx.buff_size, subcarrier_len,
                    peer->csi_data_buff->seq_id, peer->csi_data_buff->timestamp);
            }
            if (presence_failure_active() && !presence_trace_matches(peer->mac)) {
                buff_index.failure_token=presence_failure_enqueue(subcarrier_len,buff_index.begin,buff_index.window,peer->window_ctx.buff_size,(const float *const *)peer->csi_data_buff->amplitude);
            }""",
        ),
        (
            "    esp_err_t ret = ESP_FAIL;\n\n    if (peer->window_ctx.ts_delta_relaxed",
            "    esp_err_t ret = ESP_FAIL;\n    uint32_t sampling_reason = 4; /* Accumulating; diagnostic only. */\n\n    if (peer->window_ctx.ts_delta_relaxed",
        ),
        (
            "    if (time_tamp < 0 || time_tamp > ts_limit) {",
            "    if (time_tamp < 0 || time_tamp > ts_limit) {\n        sampling_reason = 1;",
        ),
        (
            "        if (buff_index->window < peer->window_ctx.handle_window / 3) {",
            "        if (buff_index->window < peer->window_ctx.handle_window / 3) {\n            sampling_reason = 2;",
        ),
        (
            "        ret = ESP_OK;\n        peer->window_ctx.window_start_seq += buff_index->window / 2;",
            "        ret = ESP_OK;\n        sampling_reason = 3;\n        peer->window_ctx.window_start_seq += buff_index->window / 2;",
        ),
        (
            "UPDATE_STATE:\n    peer->window_ctx.last_timestamp",
            """UPDATE_STATE:
    if (presence_sampling_active()) {
        presence_sampling_window(peer->mac, sampling_reason,
            peer->csi_data_buff->seq_id[buff_index->end],
            peer->csi_data_buff->timestamp[buff_index->end], buff_index->window,
            spent_time, time_tamp, (uint32_t)ts_limit,
            peer->window_ctx.handle_window, s_ctx.dec_config.csi_handle_time);
    }
    peer->window_ctx.last_timestamp""",
        ),
        (
            "    return esp_radar_motion_dec_compute(cols, csi_data_0_len, csi_data_0, csi_data_1_len, csi_data_1, motion_dec_buff_current);",
            """    if (presence_trace_active() && presence_trace_matches(peer->mac)) {
        return presence_trace_compute(buff_index->trace_token, cols, csi_data_0_len, csi_data_1_len, motion_dec_buff_current);
    }
    if (presence_failure_active()) return presence_failure_compute(buff_index->failure_token,cols,csi_data_0_len,csi_data_1_len,motion_dec_buff_current);
    return esp_radar_motion_dec_compute(cols, csi_data_0_len, csi_data_0, csi_data_1_len, csi_data_1, motion_dec_buff_current);""",
        ),
        (
            "            if (peer->cal->calibrate_status == RADAR_CALIBRATE_PROGRESS && peer->motion_dec_buff_num >= 2) {",
            """            if (presence_trace_active() && presence_trace_matches(peer->mac)) presence_trace_basis(buff_index.trace_token, peer->waveform_wander_last);
            if (peer->cal->calibrate_status == RADAR_CALIBRATE_PROGRESS && peer->motion_dec_buff_num >= 2) {""",
        ),
        (
            "        int32_t time_spent  = peer->csi_data_buff->timestamp[buff_index.end]",
            """        presence_trace_decision_sdk(buff_index.trace_token, peer->mac, peer, &radar_info);
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
    if not re.search(r"\bcall8\s+[0-9a-f]+ <presence_source_admit>", disassembly):
        raise ValueError("RX path bypasses source admission")
    if disassembly.index("<presence_source_admit>") > disassembly.index("<__wrap_esp_csi_gain_ctrl_compensate_rx_gain>"):
        raise ValueError("source admission follows compensation")
    return {
        "source_admission_precedes_compensation": True,
        "rx_calls_compensation_wrapper": True,
        "rx_disassembly_sha256": hashlib.sha256(disassembly.encode()).hexdigest(),
    }


SENSING_SHA256 = "a51b5b183ec3cbd903d80b53748c0174ec039b76ac258c93110b49a06ad6c206"


def sensing(source):
    """Observe original reply/timeout callbacks; leave ping pacing unchanged."""
    if hashlib.sha256(source).hexdigest() != SENSING_SHA256:
        raise ValueError("sampling hooks require exact pinned sensing source")
    text = source.decode()
    edits = [
        ('#include "ping/ping_sock.h"',
         '#include "ping/ping_sock.h"\n#include "../../../main/trace.h"'),
        ("        fsm->ping_success_count++;",
         "        fsm->ping_success_count++;\n        presence_sampling_ping_sdk(hdl, 1);"),
        ("        fsm->ping_timeout_count++;",
         "        fsm->ping_timeout_count++;\n        presence_sampling_ping_sdk(hdl, 2);"),
    ]
    for before, after in edits:
        if text.count(before) != 1:
            raise ValueError("ambiguous sampling insertion")
        text = text.replace(before, after)
    return text.encode()


def require_sampling_observers(disassembly):
    """Require the actual callback/window paths, not just exported symbols."""
    import re
    functions = {}
    for name, body in re.findall(
        r"^[0-9a-f]+ <([^\n]+)>:\n(.*?)(?=^[0-9a-f]+ <|\Z)",
        disassembly, re.M | re.S,
    ):
        functions.setdefault(name, []).append(body)
    required = {
        "hms_ping_on_success": "presence_sampling_ping_sdk",
        "hms_ping_on_timeout": "presence_sampling_ping_sdk",
        "presence_sampling_ping_sdk": "presence_sampling_ping",
        "csi_window_update": "presence_sampling_window",
    }
    for caller, callee in required.items():
        bodies = functions.get(caller, [])
        if len(bodies) != 1 or not re.search(
            r"\bcall8\s+[0-9a-f]+ <" + re.escape(callee) + r">", bodies[0]
        ):
            raise ValueError("missing actual sampling observer: " + caller)
    if len(re.findall(r"\bcall8\s+[0-9a-f]+ <esp_ping_get_profile>",
                      functions["presence_sampling_ping_sdk"][0])) != 4:
        raise ValueError("missing public ping profile reads")
    return {"all_four_native_sampling_call_paths_verified": True,
            "public_ping_profile_reads": 4,
            "native_disassembly_sha256": hashlib.sha256(disassembly.encode()).hexdigest()}
