/* Mechanical translation of bounded Rust labels to native scheduler notes. */
#include <nuttx/config.h>
#include <nuttx/sched_note.h>
#include <stddef.h>
#include <stdint.h>

void acquisition_trace_event(uint8_t edge, const uint8_t *label, size_t length)
{
#ifdef CONFIG_SCHED_INSTRUMENTATION_DUMP
  uint8_t event;
  switch (edge)
    {
      case 'B': event = NOTE_DUMP_BEGIN; break;
      case 'E': event = NOTE_DUMP_END; break;
      case 'I': event = NOTE_DUMP_MARK; break;
      default: return;
    }
  sched_note_event(NOTE_TAG_ALWAYS, event, label, length > 96 ? 96 : length);
#else
  (void)edge;
  (void)label;
  (void)length;
#endif
}
