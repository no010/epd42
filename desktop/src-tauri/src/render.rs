use epd42_core::face::{self, FaceState};
use crate::session::Document;

// Desktop task descriptions are intentionally not part of the e-ink frame.
pub fn frame(doc: &Document) -> Result<Vec<u8>, String> {
    let s = &doc.state;
    let face = FaceState { phase: match s.phase.as_str() { "short_break" => 1, "long_break" => 2, _ => 0 },
        phase_seconds: s.phase_seconds.ceil() as u32, remaining: s.remaining.ceil() as u32,
        running: s.running, pomodoro_count: s.pomodoro_count, cycle_total: s.cycle_total,
        rounds: s.rounds, stamp: chrono::Local::now().format("%m-%d %H:%M").to_string() };
    Ok(face::render(&face))
}
