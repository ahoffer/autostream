//! UTC wall-clock text without a calendar dependency.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// `HH:MM:SS` for log lines.
pub fn clock_text(now: SystemTime) -> String {
    let (_, _, _, hour, minute, second, _) = civil_parts(now);
    format!("{hour:02}:{minute:02}:{second:02}")
}

/// The timestamp form Python's `datetime.isoformat()` gives for an aware UTC
/// time: microseconds appear only when they are not zero.
pub fn iso_timestamp(now: SystemTime) -> String {
    let (year, month, day, hour, minute, second, microsecond) = civil_parts(now);
    let fraction = if microsecond == 0 {
        String::new()
    } else {
        format!(".{microsecond:06}")
    };
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}{fraction}+00:00")
}

fn civil_parts(now: SystemTime) -> (i64, u32, u32, u32, u32, u32, u32) {
    let since_epoch = now.duration_since(UNIX_EPOCH).unwrap_or(Duration::ZERO);
    let seconds = since_epoch.as_secs() as i64;
    let microsecond = since_epoch.subsec_micros();
    let days = seconds.div_euclid(86_400);
    let day_seconds = seconds.rem_euclid(86_400) as u32;
    let (year, month, day) = civil_from_days(days);
    (
        year,
        month,
        day,
        day_seconds / 3600,
        day_seconds % 3600 / 60,
        day_seconds % 60,
        microsecond,
    )
}

/// Proleptic Gregorian date for a day count since 1970-01-01 (Howard Hinnant's algorithm).
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let shifted_month = (5 * day_of_year + 2) / 153;
    let day = (day_of_year - (153 * shifted_month + 2) / 5 + 1) as u32;
    let month = if shifted_month < 10 {
        shifted_month + 3
    } else {
        shifted_month - 9
    } as u32;
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(seconds: u64, micros: u32) -> SystemTime {
        UNIX_EPOCH + Duration::new(seconds, micros * 1000)
    }

    #[test]
    fn formats_known_instants() {
        assert_eq!(iso_timestamp(at(0, 0)), "1970-01-01T00:00:00+00:00");
        assert_eq!(
            iso_timestamp(at(1_762_787_045, 123_456)),
            "2025-11-10T15:04:05.123456+00:00"
        );
        assert_eq!(
            iso_timestamp(at(951_782_400, 0)),
            "2000-02-29T00:00:00+00:00"
        );
        assert_eq!(clock_text(at(1_762_787_045, 0)), "15:04:05");
    }
}
