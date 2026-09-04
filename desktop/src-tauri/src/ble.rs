//! EPD42 的 BLE 流式推送（移植自 tools/epd-monitor/ble_client.py）。
//!
//! 流控靠通知而不是 GATT 写应答：SoftDevice 替应用回写，所以写应答只证明包到
//! 了链路层；BEGIN/END 的应答由设备在真正初始化/刷新面板之后发出，因此这里
//! 在收到相应应答前会一直等。btleplug 0.11 里 `subscribe()` 只负责开启通知，
//! 数据从 `peripheral.notifications()` 的事件流里取（`ValueNotification`）。

use std::pin::Pin;
use std::time::Duration;

use btleplug::api::{
    Central, Characteristic, Manager as _, Peripheral as _, ScanFilter, ValueNotification,
    WriteType,
};
use btleplug::platform::{Adapter, Manager, Peripheral};
use futures::{Stream, StreamExt};
use serde::Serialize;
use uuid::Uuid;

use epd42_core as core;

const ACK_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Serialize, Clone)]
pub struct DeviceInfo {
    pub address: String,
    pub name: String,
    pub rssi: Option<i16>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PushReport {
    pub planes: usize,
    pub payload_bytes: usize,
    pub encoded_bytes: usize,
    pub packets: usize,
    pub checksum: u32,
}

async fn first_adapter(manager: &Manager) -> Result<Adapter, String> {
    let adapters = manager.adapters().await.map_err(|e| e.to_string())?;
    adapters
        .into_iter()
        .next()
        .ok_or_else(|| "没有可用的蓝牙适配器".to_string())
}

/// 扫描一段后停止；`timeout_ms` 是扫描窗口——越久越容易抓到慢速广播的设备。
async fn rescan(adapter: &Adapter, timeout_ms: u64) -> Result<(), String> {
    adapter
        .start_scan(ScanFilter::default())
        .await
        .map_err(|e| e.to_string())?;
    tokio::time::sleep(Duration::from_millis(timeout_ms.max(1000))).await;
    let _ = adapter.stop_scan().await;
    Ok(())
}

/// 扫描并把附近设备列出来，RSSI 强信号在前。
pub async fn scan_devices(timeout_secs: u64) -> Result<Vec<DeviceInfo>, String> {
    let manager = Manager::new().await.map_err(|e| e.to_string())?;
    let adapter = first_adapter(&manager).await?;
    rescan(&adapter, timeout_secs.max(1) * 1000).await?;

    let mut devices = Vec::new();
    for peripheral in adapter.peripherals().await.map_err(|e| e.to_string())? {
        if let Some(props) = peripheral.properties().await.map_err(|e| e.to_string())? {
            devices.push(DeviceInfo {
                address: format!("{}", peripheral.address()),
                name: props.local_name.unwrap_or_default(),
                rssi: props.rssi,
            });
        }
    }
    devices.sort_by_key(|d| std::cmp::Reverse(d.rssi.unwrap_or(i16::MIN)));
    Ok(devices)
}

fn find_characteristic(peripheral: &Peripheral) -> Result<Characteristic, String> {
    let uuid: Uuid = core::EPD_CHARACTERISTIC_UUID
        .parse()
        .map_err(|e| format!("特征值 UUID 解析失败：{e}"))?;
    peripheral
        .characteristics()
        .iter()
        .find(|c| c.uuid == uuid)
        .cloned()
        .ok_or_else(|| "设备上没找到 EPD 特征值（62750002-...）".to_string())
}

/// 等一个指定命令的应答通知；无关通知（比如刚订阅时的引脚配置包）直接跳过。
async fn wait_ack<S>(
    notifications: &mut S,
    characteristic_uuid: Uuid,
    command: u8,
) -> Result<Vec<u8>, String>
where
    S: Stream<Item = ValueNotification> + Unpin,
{
    let deadline = tokio::time::Instant::now() + ACK_TIMEOUT;
    loop {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        match tokio::time::timeout(remaining, notifications.next()).await {
            Err(_) => return Err(format!("等不到 0x{command:02x} 的应答（{ACK_TIMEOUT:?}）")),
            Ok(None) => return Err("通知流意外结束".to_string()),
            Ok(Some(notification)) => {
                if notification.uuid == characteristic_uuid
                    && notification.value.first() == Some(&command)
                {
                    return Ok(notification.value);
                }
            }
        }
    }
}

fn check_status(ack: &[u8], what: &str) -> Result<(), String> {
    match ack.get(1) {
        Some(&core::STATUS_OK) => Ok(()),
        Some(&status) => Err(format!("{what} 被设备拒绝：status 0x{status:02x}")),
        None => Err(format!("{what} 应答太短：{} 字节", ack.len())),
    }
}

async fn stream_planes<S>(
    peripheral: &Peripheral,
    characteristic: &Characteristic,
    notifications: &mut S,
    planes: &[Vec<u8>],
) -> Result<PushReport, String>
where
    S: Stream<Item = ValueNotification> + Unpin,
{
    let payload_bytes: usize = planes.iter().map(|p| p.len()).sum();
    let mut encoded_bytes = 0usize;
    let mut packets = 0usize;

    for (index, plane) in planes.iter().enumerate() {
        // 只有最后一平面触发刷新（三色屏在 0x10/0x13 之间刷新会清掉前一平面）
        let flags = if index + 1 == planes.len() {
            core::FLAG_REFRESH
        } else {
            0
        };

        peripheral
            .write(
                characteristic,
                &[core::CMD_STREAM_BEGIN, index as u8],
                WriteType::WithResponse,
            )
            .await
            .map_err(|e| e.to_string())?;
        let ack = wait_ack(notifications, characteristic.uuid, core::CMD_STREAM_BEGIN).await?;
        check_status(&ack, "STREAM_BEGIN")?;

        let encoded = core::packbits_encode(plane);
        for chunk in core::chunks(&encoded) {
            peripheral
                .write(characteristic, &chunk, WriteType::WithResponse)
                .await
                .map_err(|e| e.to_string())?;
            packets += 1;
        }
        encoded_bytes += encoded.len();

        peripheral
            .write(
                characteristic,
                &core::end_request(plane, flags),
                WriteType::WithResponse,
            )
            .await
            .map_err(|e| e.to_string())?;
        let ack = wait_ack(notifications, characteristic.uuid, core::CMD_STREAM_END).await?;
        check_status(&ack, "STREAM_END")?;
    }

    Ok(PushReport {
        planes: planes.len(),
        payload_bytes,
        encoded_bytes,
        packets,
        checksum: core::checksum(&planes[0]),
    })
}

/// 推送一次画面：`luma` 是 400x300 灰度像素（>127 = 白纸），由前端 canvas 提供。
pub async fn push_frame(
    address: Option<&str>,
    luma: &[u8],
    driver: u8,
    scan_timeout_secs: u64,
) -> Result<PushReport, String> {
    if luma.len() != core::SCREEN_WIDTH * core::SCREEN_HEIGHT {
        return Err(format!(
            "像素数据需要 {}*{} = {} 个字节，收到 {}",
            core::SCREEN_WIDTH,
            core::SCREEN_HEIGHT,
            core::SCREEN_WIDTH * core::SCREEN_HEIGHT,
            luma.len()
        ));
    }

    let manager = Manager::new().await.map_err(|e| e.to_string())?;
    let adapter = first_adapter(&manager).await?;

    let peripheral = match address {
        Some(addr) => {
            let found = adapter
                .peripherals()
                .await
                .map_err(|e| e.to_string())?
                .into_iter()
                .find(|p| format!("{}", p.address()).eq_ignore_ascii_case(addr));
            match found {
                Some(p) => p,
                None => {
                    // 缓存里没有：用可配置的扫描窗口再找
                    rescan(&adapter, scan_timeout_secs.max(1) * 1000).await?;
                    adapter
                        .peripherals()
                        .await
                        .map_err(|e| e.to_string())?
                        .into_iter()
                        .find(|p| format!("{}", p.address()).eq_ignore_ascii_case(addr))
                        .ok_or_else(|| format!("找不到设备 {addr}，请先扫描"))?
                }
            }
        }
        None => {
            rescan(&adapter, scan_timeout_secs.max(1) * 1000).await?;
            let mut candidates: Vec<(Peripheral, String)> = Vec::new();
            for peripheral in adapter.peripherals().await.map_err(|e| e.to_string())? {
                let name = peripheral
                    .properties()
                    .await
                    .ok()
                    .flatten()
                    .and_then(|props| props.local_name)
                    .unwrap_or_default();
                candidates.push((peripheral, name));
            }
            candidates
                .into_iter()
                .find(|(_, name)| name.starts_with("NRF_EPD"))
                .map(|(p, _)| p)
                .ok_or_else(|| "扫描范围内没有 NRF_EPD 设备，请先扫描并选择设备".to_string())?
        }
    };

    peripheral.connect().await.map_err(|e| e.to_string())?;
    peripheral
        .discover_services()
        .await
        .map_err(|e| e.to_string())?;
    let characteristic = find_characteristic(&peripheral)?;

    // 开启通知 + 取得通知流（btleplug 0.11 的事件式 API）
    peripheral
        .subscribe(&characteristic)
        .await
        .map_err(|e| e.to_string())?;
    let mut notifications: Pin<Box<dyn Stream<Item = ValueNotification> + Send>> = peripheral
        .notifications()
        .await
        .map_err(|e| e.to_string())?;

    let planes = core::pack_planes(luma, driver);
    let result =
        stream_planes(&peripheral, &characteristic, &mut notifications, &planes).await;
    let _ = peripheral.disconnect().await;
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    // 前端按驼峰读取推送结果：一旦回退成蛇形，状态行就会显示 "undefined 字节"
    #[test]
    fn push_report_serializes_camel_case() {
        let report = PushReport {
            planes: 1,
            payload_bytes: 15000,
            encoded_bytes: 1650,
            packets: 87,
            checksum: 12345,
        };
        let json = serde_json::to_string(&report).expect("序列化失败");
        assert!(json.contains("\"payloadBytes\""), "应为驼峰 payloadBytes：{json}");
        assert!(json.contains("\"encodedBytes\""), "应为驼峰 encodedBytes：{json}");
        assert!(!json.contains("payload_bytes"), "不应再有蛇形 payload_bytes：{json}");
        assert!(!json.contains("encoded_bytes"), "不应再有蛇形 encoded_bytes：{json}");
    }
}