//! EPD42 上位机协议核心（桌面版）。
//!
//! 与 `tools/epd-monitor/protocol.py` 和 `render.pack_plane` 保持同一约定：
//!
//! * MSB 是字节最左像素；
//! * bit 1 = 白纸，bit 0 = 黑墨；
//! * 一平面 = `50 * 300 = 15000` 字节（400x300 屏，宽度 8 对齐）；
//! * 平面先做 TIFF PackBits 游程编码再分包（每包 = 命令字节 + 最多 19 字节载荷，
//!   ATT 的 20 字节上限，S110/S130 是蓝牙 4.1 无法协商更大 MTU）。
//!
//! 这一层不依赖任何第三方 crate，便于离线单测。

pub const SCREEN_WIDTH: usize = 400;
pub const SCREEN_HEIGHT: usize = 300;
pub const LINE_BYTES: usize = SCREEN_WIDTH / 8;
pub const PLANE_BYTES: usize = LINE_BYTES * SCREEN_HEIGHT;

/// 128 位形式的厂商服务 UUID（固件 BLE_EPD_BASE_UUID，短 ID 0x0001）。
pub const EPD_SERVICE_UUID: &str = "62750001-d828-918d-fb46-b6c11c675aec";
/// 特征值：62750002-...（命令 + 数据流都走这一个 characteristic）。
pub const EPD_CHARACTERISTIC_UUID: &str = "62750002-d828-918d-fb46-b6c11c675aec";

pub const CMD_INIT: u8 = 0x01; // 带驱动 id
pub const CMD_SLEEP: u8 = 0x06;
pub const CMD_STREAM_BEGIN: u8 = 0xB0;
pub const CMD_STREAM_DATA: u8 = 0xB1;
pub const CMD_STREAM_END: u8 = 0xB2;
pub const CMD_STREAM_ABORT: u8 = 0xB3;
pub const CMD_GET_STATUS: u8 = 0xB5;

pub const FLAG_REFRESH: u8 = 0x01;
pub const FLAG_SLEEP: u8 = 0x02;

pub const STATUS_OK: u8 = 0x00;

/// 一次 GATT 写入的载荷上限（命令字节之外）。
pub const DATA_CHUNK: usize = 19;

/// 固件在 EPD_CMD_STREAM_END 里校验的整行字节和。
pub fn checksum(plane: &[u8]) -> u32 {
    plane.iter().fold(0u32, |acc, &b| acc.wrapping_add(b as u32))
}

/// 把 400x300 灰度像素（0..255，>127 视为白纸）打包成一平面字节。
pub fn pack_plane(luma: &[u8]) -> Vec<u8> {
    assert_eq!(
        luma.len(),
        SCREEN_WIDTH * SCREEN_HEIGHT,
        "pack_plane 需要 400*300 个像素，收到 {}",
        luma.len()
    );
    let mut plane = vec![0u8; PLANE_BYTES];
    for row in 0..SCREEN_HEIGHT {
        let offset = row * SCREEN_WIDTH;
        for byte_index in 0..LINE_BYTES {
            let mut bits = 0u8;
            for bit in 0..8 {
                if luma[offset + byte_index * 8 + bit] > 127 {
                    bits |= 0x80 >> bit;
                }
            }
            plane[row * LINE_BYTES + byte_index] = bits;
        }
    }
    plane
}

/// 按驱动打包平面：b/w 驱动 1 个平面，三色（driver 3）第 2 个平面为全白红通道。
pub fn pack_planes(luma: &[u8], driver: u8) -> Vec<Vec<u8>> {
    let mut planes = vec![pack_plane(luma)];
    if driver == 3 {
        planes.push(vec![0xFF; PLANE_BYTES]);
    }
    planes
}

/// TIFF PackBits 编码（无行尾标记），移植自 `protocol.packbits_encode`。
pub fn packbits_encode(plane: &[u8]) -> Vec<u8> {
    let mut out: Vec<u8> = Vec::new();
    let total = plane.len();
    let mut index = 0;
    while index < total {
        let byte = plane[index];
        let mut run = 1usize;
        while run < 128 && index + run < total && plane[index + run] == byte {
            run += 1;
        }
        if run >= 3 {
            out.push((257 - run) as u8);
            out.push(byte);
            index += run;
            continue;
        }
        let mut literal: Vec<u8> = Vec::new();
        while index < total && literal.len() < 128 {
            let mut lookahead = 1usize;
            while lookahead < 128
                && index + lookahead < total
                && plane[index + lookahead] == plane[index]
            {
                lookahead += 1;
            }
            if lookahead >= 3 {
                break;
            }
            literal.push(plane[index]);
            index += 1;
        }
        out.push((literal.len() - 1) as u8);
        out.extend_from_slice(&literal);
    }
    out
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum DecodeMode {
    Control,
    Literal,
    RunValue,
}

/// 增量 PackBits 解码器：GATT 包可以在控制字节和它管辖的载荷字节之间断开，
/// 逐片喂入与整段喂入得到的结果必须一致（固件状态机也是这样工作的）。
pub struct PackbitsDecoder {
    out: Vec<u8>,
    mode: DecodeMode,
    left: usize,
}

impl Default for PackbitsDecoder {
    fn default() -> Self {
        Self::new()
    }
}

impl PackbitsDecoder {
    pub fn new() -> Self {
        PackbitsDecoder {
            out: Vec::new(),
            mode: DecodeMode::Control,
            left: 0,
        }
    }

    pub fn feed(&mut self, data: &[u8]) {
        for &byte in data {
            match self.mode {
                DecodeMode::Literal => {
                    self.out.push(byte);
                    self.left -= 1;
                    if self.left == 0 {
                        self.mode = DecodeMode::Control;
                    }
                }
                DecodeMode::RunValue => {
                    self.out.extend(std::iter::repeat(byte).take(self.left));
                    self.mode = DecodeMode::Control;
                }
                DecodeMode::Control => {
                    if byte == 128 {
                        continue; // PackBits 空操作
                    } else if byte < 128 {
                        self.mode = DecodeMode::Literal;
                        self.left = usize::from(byte) + 1;
                    } else {
                        self.mode = DecodeMode::RunValue;
                        self.left = 257 - usize::from(byte);
                    }
                }
            }
        }
    }

    pub fn decoded(&self) -> &[u8] {
        &self.out
    }
}

pub fn packbits_decode(encoded: &[u8]) -> Vec<u8> {
    let mut decoder = PackbitsDecoder::new();
    decoder.feed(encoded);
    decoder.decoded().to_vec()
}

/// 流式数据包迭代器：`[0xB1] + 最多 DATA_CHUNK 字节`。
pub struct Chunks<'a> {
    data: &'a [u8],
    offset: usize,
}

impl<'a> Iterator for Chunks<'a> {
    type Item = Vec<u8>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.offset >= self.data.len() {
            return None;
        }
        let end = (self.offset + DATA_CHUNK).min(self.data.len());
        let mut packet = Vec::with_capacity(end - self.offset + 1);
        packet.push(CMD_STREAM_DATA);
        packet.extend_from_slice(&self.data[self.offset..end]);
        self.offset = end;
        Some(packet)
    }
}

pub fn chunks(data: &[u8]) -> Chunks<'_> {
    Chunks { data, offset: 0 }
}

/// 构建 EPD_CMD_STREAM_END 请求：长度与和值描述**解码后**的整平面，
/// 因此编码是否压缩对校验不可见。
pub fn end_request(plane: &[u8], flags: u8) -> Vec<u8> {
    let mut request = vec![CMD_STREAM_END];
    request.extend_from_slice(&(plane.len() as u16).to_le_bytes());
    request.extend_from_slice(&checksum(plane).to_le_bytes());
    request.push(flags);
    request
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check(condition: bool, description: &str) {
        assert!(condition, "{}", description);
    }

    /// 确定性伪随机字节流，替代 rand 依赖。
    fn lcg(seed: u64, n: usize) -> Vec<u8> {
        let mut s = seed;
        (0..n)
            .map(|_| {
                s = s
                    .wrapping_mul(6364136223846793005)
                    .wrapping_add(1442695040888963407);
                (s >> 33) as u8
            })
            .collect()
    }

    #[test]
    fn geometry() {
        check(SCREEN_WIDTH % 8 == 0, "宽度按字节对齐，一行不会跨两个字节");
        check(LINE_BYTES * SCREEN_HEIGHT == PLANE_BYTES, "一平面 = 50*300 = 15000 字节");
    }

    #[test]
    fn bit_packing() {
        // 全白纸 -> 全 0xFF；全黑墨 -> 全 0x00
        assert!(pack_plane(&vec![255; 120_000]).iter().all(|&b| b == 0xFF));
        assert!(pack_plane(&vec![0; 120_000]).iter().all(|&b| b == 0));

        // 一枚黑像素 (3,1)：行 1 第 0 字节的 MSB-first 第 4 位清 0
        let mut luma = vec![255u8; 120_000];
        luma[1 * SCREEN_WIDTH + 3] = 0;
        let plane = pack_plane(&luma);
        assert_eq!(plane[LINE_BYTES], 0xFF ^ 0x10);
        assert_eq!(
            checksum(&plane),
            (PLANE_BYTES as u32 * 0xFF) - 0x10,
            "平面里别的东西没有变"
        );

        // 右下角 (399,299) 落在最后一个字节的最低位
        let mut luma = vec![255u8; 120_000];
        luma[299 * SCREEN_WIDTH + 399] = 0;
        let plane = pack_plane(&luma);
        assert_eq!(*plane.last().unwrap(), 0xFE);
    }

    #[test]
    fn packbits_roundtrip() {
        let cases: Vec<Vec<u8>> = vec![
            vec![0xFF; PLANE_BYTES],
            vec![0x00; PLANE_BYTES],
            lcg(1, PLANE_BYTES),
            lcg(7, PLANE_BYTES),
            {
                let mut v = Vec::with_capacity(PLANE_BYTES);
                for i in 0..PLANE_BYTES {
                    v.push((i % 7) as u8);
                }
                v
            },
            Vec::new(),
        ];
        for (idx, plane) in cases.iter().enumerate() {
            let encoded = packbits_encode(plane);
            assert_eq!(packbits_decode(&encoded), *plane, "roundtrip case {}", idx);
        }
    }

    #[test]
    fn packbits_incremental_feed_matches_whole() {
        let plane = lcg(3, PLANE_BYTES);
        let encoded = packbits_encode(&plane);
        // 逐片喂入（7/13 字节一片），模拟 GATT 包在任意位置断开
        for step in [7usize, 13] {
            let mut decoder = PackbitsDecoder::new();
            for slice in encoded.chunks(step) {
                decoder.feed(slice);
            }
            assert_eq!(decoder.decoded(), plane.as_slice(), "step {}", step);
        }
    }

    #[test]
    fn chunking_fits_att() {
        let plane = lcg(9, PLANE_BYTES);
        let encoded = packbits_encode(&plane);
        let packets: Vec<Vec<u8>> = chunks(&encoded).collect();
        assert!(
            packets.iter().all(|p| p.len() <= DATA_CHUNK + 1),
            "每包 <= 20 字节（命令 + 19 载荷）"
        );
        assert!(packets.iter().all(|p| p[0] == CMD_STREAM_DATA));
        // 载荷拼接还原
        let mut joined = Vec::new();
        for p in &packets {
            joined.extend_from_slice(&p[1..]);
        }
        assert_eq!(joined, encoded);
    }

    #[test]
    fn end_request_fields() {
        let plane = vec![0xAA; 100];
        let req = end_request(&plane, FLAG_REFRESH);
        assert_eq!(req.len(), 8);
        assert_eq!(req[0], CMD_STREAM_END);
        assert_eq!(&req[1..3], &100u16.to_le_bytes());
        assert_eq!(&req[3..7], &checksum(&plane).to_le_bytes());
        assert_eq!(req[7], FLAG_REFRESH);
    }

    #[test]
    fn planes_per_driver() {
        let luma = vec![255u8; 120_000];
        assert_eq!(pack_planes(&luma, 1).len(), 1);
        assert_eq!(pack_planes(&luma, 2).len(), 1);
        let bwr = pack_planes(&luma, 3);
        assert_eq!(bwr.len(), 2);
        assert!(bwr[1].iter().all(|&b| b == 0xFF), "三色屏第 2 平面全白白通道");
    }
}