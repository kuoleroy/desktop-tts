use tauri_plugin_global_shortcut::{Code, Modifiers};

pub fn parse_shortcut(s: &str) -> Option<(Modifiers, Code)> {
    let parts: Vec<&str> = s.split('+').map(|p| p.trim()).collect();
    let (key, mods) = parts.split_last()?;
    let mut m = Modifiers::empty();
    for p in mods {
        m |= match p.to_lowercase().as_str() {
            "ctrl" | "control" => Modifiers::CONTROL,
            "shift" => Modifiers::SHIFT,
            "alt" => Modifiers::ALT,
            "super" | "win" | "cmd" => Modifiers::SUPER,
            _ => return None,
        };
    }
    let code = parse_code(key.trim())?;
    Some((m, code))
}

fn parse_code(k: &str) -> Option<Code> {
    use Code::*;
    if k.len() == 1 && k.as_bytes()[0].is_ascii_alphabetic() {
        let c = k.to_ascii_uppercase();
        return Some(match c.as_str() {
            "A" => KeyA, "B" => KeyB, "C" => KeyC, "D" => KeyD, "E" => KeyE, "F" => KeyF,
            "G" => KeyG, "H" => KeyH, "I" => KeyI, "J" => KeyJ, "K" => KeyK, "L" => KeyL,
            "M" => KeyM, "N" => KeyN, "O" => KeyO, "P" => KeyP, "Q" => KeyQ, "R" => KeyR,
            "S" => KeyS, "T" => KeyT, "U" => KeyU, "V" => KeyV, "W" => KeyW, "X" => KeyX,
            "Y" => KeyY, "Z" => KeyZ, _ => return None,
        });
    }
    Some(match k.to_lowercase().as_str() {
        "space" => Space, "enter" => Enter, "escape" | "esc" => Escape, "tab" => Tab,
        "backspace" => Backspace, "delete" => Delete, "insert" => Insert, "home" => Home,
        "end" => End, "pageup" => PageUp, "pagedown" => PageDown,
        "f1" => F1, "f2" => F2, "f3" => F3, "f4" => F4, "f5" => F5, "f6" => F6,
        "f7" => F7, "f8" => F8, "f9" => F9, "f10" => F10, "f11" => F11, "f12" => F12,
        "f13" => F13, "f14" => F14, "f15" => F15, "f16" => F16,
        "up" => ArrowUp, "down" => ArrowDown, "left" => ArrowLeft, "right" => ArrowRight,
        "0" => Digit0, "1" => Digit1, "2" => Digit2, "3" => Digit3, "4" => Digit4,
        "5" => Digit5, "6" => Digit6, "7" => Digit7, "8" => Digit8, "9" => Digit9,
        _ => return None,
    })
}