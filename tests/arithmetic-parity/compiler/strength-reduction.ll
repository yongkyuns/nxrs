; Minimal pass-level reproducer for the u64 recurrence's retained IV multiply.
; Run only loop-reduce: a full optimizer can fold this diagnostic's final sum.
; n32 is the real target's native-integer declaration. n32:64 is an analysis
; counterfactual only; never use it to compile Xtensa firmware.
target datalayout = "e-m:e-p:32:32-v1:8:8-i64:64-i128:128-n32"
target triple = "xtensa-unknown-none-elf"

define i32 @native_counter() {
entry:
  br label %loop
loop:
  %i = phi i32 [ 0, %entry ], [ %next, %loop ]
  %s = phi i32 [ 0, %entry ], [ %sum, %loop ]
  %next = add nuw nsw i32 %i, 1
  %product = mul i32 %i, 2135587861
  %sum = add i32 %s, %product
  %more = icmp ult i32 %i, 63
  br i1 %more, label %loop, label %exit
exit:
  ret i32 %sum
}

define i64 @wide_counter() {
entry:
  br label %loop
loop:
  %i = phi i64 [ 0, %entry ], [ %next, %loop ]
  %s = phi i64 [ 0, %entry ], [ %sum, %loop ]
  %next = add nuw nsw i64 %i, 1
  %product = mul nuw nsw i64 %i, 2135587861
  %sum = add i64 %s, %product
  %more = icmp ult i64 %i, 63
  br i1 %more, label %loop, label %exit
exit:
  ret i64 %sum
}
