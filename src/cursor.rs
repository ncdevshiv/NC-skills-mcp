use anyhow::Result;
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize)]
struct CursorPayload {
    offset: usize,
}

pub fn encode_cursor(offset: usize) -> String {
    let payload = CursorPayload { offset };
    let json = serde_json::to_vec(&payload).unwrap_or_else(|_| b"{}".to_vec());
    URL_SAFE_NO_PAD.encode(json)
}

pub fn decode_cursor(cursor: Option<&str>) -> Result<usize> {
    match cursor {
        None | Some("") => Ok(0),
        Some(s) => {
            let decoded = URL_SAFE_NO_PAD
                .decode(s)
                .map_err(|_| anyhow::anyhow!("Invalid cursor"))?;
            let payload: CursorPayload =
                serde_json::from_slice(&decoded).map_err(|_| anyhow::anyhow!("Invalid cursor"))?;
            Ok(payload.offset)
        }
    }
}

pub fn paginate<T: Clone>(
    items: &[T],
    cursor: Option<&str>,
    limit: Option<usize>,
) -> Result<(Vec<T>, Option<String>)> {
    use crate::config::{DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE};
    let lim = limit.unwrap_or(DEFAULT_PAGE_SIZE).clamp(1, MAX_PAGE_SIZE);
    let offset = decode_cursor(cursor)?;
    let end = (offset + lim).min(items.len());
    let page = if offset < items.len() {
        items[offset..end].to_vec()
    } else {
        Vec::new()
    };
    let next = if end < items.len() {
        Some(encode_cursor(end))
    } else {
        None
    };
    Ok((page, next))
}
