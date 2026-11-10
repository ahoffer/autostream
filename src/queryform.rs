//! The `?key=value` form of a request, parsed the way Python's `parse_qs` did.

use indexmap::IndexMap;

/// Decoded query pairs, every value kept, in request order.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct QueryForm {
    // Keyed by parameter name because the API addresses values by name; a key
    // may repeat and the first occurrence wins, exactly as parse_qs lists them.
    values: IndexMap<String, Vec<String>>,
}

impl QueryForm {
    /// Percent-decode and split a raw query string; blank values are kept.
    pub fn parse(query: &str) -> Self {
        let mut form = Self::default();
        for (key, value) in form_urlencoded::parse(query.as_bytes()) {
            form.values
                .entry(key.into_owned())
                .or_default()
                .push(value.into_owned());
        }
        form
    }

    pub fn from_pairs<'a>(pairs: impl IntoIterator<Item = (&'a str, &'a str)>) -> Self {
        let mut form = Self::default();
        for (key, value) in pairs {
            form.values
                .entry(key.to_string())
                .or_default()
                .push(value.to_string());
        }
        form
    }

    pub fn contains(&self, key: &str) -> bool {
        self.values.contains_key(key)
    }

    /// The first value given for the key, if the key was given at all.
    pub fn first(&self, key: &str) -> Option<&str> {
        self.values
            .get(key)
            .and_then(|values| values.first())
            .map(String::as_str)
    }

    /// The first value, or an empty string when absent, as the Python handlers read it.
    pub fn first_or_blank(&self, key: &str) -> &str {
        self.first(key).unwrap_or("")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_pluses_percent_escapes_blanks_and_repeats() {
        let form = QueryForm::parse("name=+ships+&file=&fps=1&fps=2&bare&x=a%2Fb");
        assert_eq!(form.first("name"), Some(" ships "));
        assert_eq!(form.first("file"), Some(""));
        assert!(form.contains("file"));
        assert_eq!(form.first("fps"), Some("1"));
        assert_eq!(form.first("bare"), Some(""));
        assert_eq!(form.first("x"), Some("a/b"));
        assert_eq!(form.first("absent"), None);
        assert_eq!(form.first_or_blank("absent"), "");
    }
}
