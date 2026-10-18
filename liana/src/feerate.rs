//! Fee rates are stored as integer satoshis per 1,000 virtual bytes.
//!
//! User-facing numbers and strings are in sat/vB, with at most three decimal places.
use std::{convert::TryFrom, fmt, str::FromStr};

use serde::{de, Deserialize, Deserializer, Serialize, Serializer};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct FeeRate(u64);

impl FeeRate {
    pub const fn from_sat_per_kvb(rate: u64) -> Self {
        Self(rate)
    }

    pub const fn from_sat_per_vb(rate: u64) -> Self {
        Self(rate.saturating_mul(1_000))
    }

    pub const fn as_sat_per_kvb(self) -> u64 {
        self.0
    }

    pub fn as_sat_per_vb(self) -> f64 {
        self.0 as f64 / 1_000.0
    }

    /// Supported user-selected rates: 0.1 through 1,000 sat/vB.
    pub const fn is_valid(self) -> bool {
        self.0 >= 100 && self.0 <= 1_000_000
    }

    /// Minimum whole-satoshi fee, rounded up. Overflow never wraps or saturates.
    pub fn fee_vbytes(self, vsize: u64) -> Option<u64> {
        u64::try_from((u128::from(self.0) * u128::from(vsize) + 999) / 1_000).ok()
    }

    /// Measured rate rounded up to the next 0.001 sat/vB. A zero size is invalid.
    /// The result may exceed the supported range; callers must validate user-selected rates.
    pub fn from_fee_vbytes(fee: u64, vsize: u64) -> Self {
        if vsize == 0 {
            return Self(u64::MAX);
        }
        Self(
            u64::try_from((u128::from(fee) * 1_000 + u128::from(vsize) - 1) / u128::from(vsize))
                .unwrap_or(u64::MAX),
        )
    }

    pub fn checked_add(self, other: Self) -> Option<Self> {
        self.0.checked_add(other.0).map(Self)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ParseFeeRateError;

impl fmt::Display for ParseFeeRateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("expected a fee rate from 0.1 to 1000 sat/vB with at most three decimal places")
    }
}
impl std::error::Error for ParseFeeRateError {}

impl FromStr for FeeRate {
    type Err = ParseFeeRateError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let (whole, fraction) = s.split_once('.').unwrap_or((s, ""));
        if s.is_empty()
            || (whole.is_empty() && fraction.is_empty())
            || !whole.bytes().all(|b| b.is_ascii_digit())
            || !fraction.bytes().all(|b| b.is_ascii_digit())
            || fraction.len() > 3
        {
            return Err(ParseFeeRateError);
        }
        let fraction_digits = fraction.len() as u32;
        let whole: u64 = if whole.is_empty() {
            0
        } else {
            whole.parse().map_err(|_| ParseFeeRateError)?
        };
        let fraction: u64 = if fraction.is_empty() {
            0
        } else {
            fraction.parse().map_err(|_| ParseFeeRateError)?
        };
        let rate = whole
            .checked_mul(1_000)
            .and_then(|whole| whole.checked_add(fraction * 10u64.pow(3 - fraction_digits)))
            .map(Self)
            .ok_or(ParseFeeRateError)?;
        if rate.is_valid() {
            Ok(rate)
        } else {
            Err(ParseFeeRateError)
        }
    }
}

impl fmt::Display for FeeRate {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0 / 1_000)?;
        let fraction = self.0 % 1_000;
        if fraction != 0 {
            write!(f, ".{}", format!("{fraction:03}").trim_end_matches('0'))?;
        }
        Ok(())
    }
}

impl Serialize for FeeRate {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        if self.0 % 1_000 == 0 {
            serializer.serialize_u64(self.0 / 1_000)
        } else {
            serializer.serialize_f64(self.as_sat_per_vb())
        }
    }
}

impl<'de> Deserialize<'de> for FeeRate {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Visitor;
        impl de::Visitor<'_> for Visitor {
            type Value = FeeRate;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str(
                    "a fee rate in sat/vB from 0.1 to 1000 with at most three decimal places",
                )
            }
            fn visit_u64<E: de::Error>(self, value: u64) -> Result<FeeRate, E> {
                value.to_string().parse().map_err(E::custom)
            }
            fn visit_i64<E: de::Error>(self, value: i64) -> Result<FeeRate, E> {
                value.to_string().parse().map_err(E::custom)
            }
            fn visit_f64<E: de::Error>(self, value: f64) -> Result<FeeRate, E> {
                value.to_string().parse().map_err(E::custom)
            }
        }
        deserializer.deserialize_any(Visitor)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_parsing_and_display() {
        for (text, rate) in [
            ("0.1", 100),
            (".125", 125),
            ("0.999", 999),
            ("1", 1_000),
            ("1.234", 1_234),
            ("1000.000", 1_000_000),
        ] {
            let parsed: FeeRate = text.parse().unwrap();
            assert_eq!(parsed.as_sat_per_kvb(), rate);
            assert_eq!(parsed.to_string().parse::<FeeRate>().unwrap(), parsed);
        }
        for text in [
            "",
            ".",
            "0",
            "-1",
            "+1",
            "NaN",
            "inf",
            "1e2",
            " 1",
            "1 ",
            "0.099",
            "0.1000",
            "1.2345",
            "1000.001",
            "18446744073709551616",
        ] {
            assert!(text.parse::<FeeRate>().is_err(), "{}", text);
        }
    }

    #[test]
    fn checked_arithmetic_and_rounding() {
        let rate = FeeRate::from_sat_per_kvb(125);
        assert_eq!(rate.fee_vbytes(1), Some(1));
        assert_eq!(rate.fee_vbytes(200), Some(25));
        assert_eq!(rate.fee_vbytes(201), Some(26));
        assert_eq!(
            FeeRate::from_sat_per_kvb(u64::MAX).fee_vbytes(u64::MAX),
            None
        );
        assert_eq!(FeeRate::from_sat_per_kvb(u64::MAX).checked_add(rate), None);
        assert_eq!(FeeRate::from_fee_vbytes(26, 201).as_sat_per_kvb(), 130);
        assert!(!FeeRate::from_fee_vbytes(0, 0).is_valid());
    }

    #[test]
    fn serde_roundtrip_and_invalid_numbers() {
        for rate in (100..=1_000_000).step_by(137) {
            let expected = FeeRate::from_sat_per_kvb(rate);
            let json = serde_json::to_string(&expected).unwrap();
            assert_eq!(serde_json::from_str::<FeeRate>(&json).unwrap(), expected);
        }
        assert_eq!(
            serde_json::to_string(&FeeRate::from_sat_per_vb(2)).unwrap(),
            "2"
        );
        for json in [
            "0", "-1", "0.099", "0.1234", "1000.001", "1e300", "null", "true", "\"0.1\"",
        ] {
            assert!(serde_json::from_str::<FeeRate>(json).is_err(), "{}", json);
        }
    }
}
