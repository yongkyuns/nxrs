/* SPDX-License-Identifier: MIT */
#include "sq_layout_padding.h"

#define SQ_STRINGIFY_INNER(value) #value
#define SQ_STRINGIFY(value) SQ_STRINGIFY_INNER(value)

/* Retained by --undefined at final link; the section contains no instructions. */
__asm__(
    ".section .text.nxrs_sq_layout_padding,\"ax\",@progbits\n"
    ".global nxrs_sq_layout_padding\n"
    ".type nxrs_sq_layout_padding,@object\n"
    "nxrs_sq_layout_padding:\n"
    ".zero " SQ_STRINGIFY(SQ_LAYOUT_PADDING_BYTES) "\n"
    ".size nxrs_sq_layout_padding, .-nxrs_sq_layout_padding\n"
    ".previous\n");
