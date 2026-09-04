//! 把 Rust 渲染的沙漏画面写成 PGM（P5），用于开发期视觉检查。
//!     cargo run -p epd42-core --example render_preview
use std::fs;

use epd42_core::face::{render, FaceState};

fn main() {
    let state = FaceState {
        phase: 0,
        phase_seconds: 1500,
        remaining: 750,
        running: true,
        pomodoro_count: 2,
        cycle_total: 7,
        rounds: 4,
        stamp: "09-04 16:30".to_string(),
    };
    let luma = render(&state);
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("workspace 目录")
        .join("target");
    fs::create_dir_all(&dir).expect("建 target 目录失败");
    let path = dir.join("face_preview.pgm");
    let mut bytes = "P5\n400 300\n255\n".as_bytes().to_vec();
    bytes.extend_from_slice(&luma);
    fs::write(&path, bytes).expect("写 PGM 失败");
    println!("written {}", path.display());
}