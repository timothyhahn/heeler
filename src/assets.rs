use std::io::{self, Write};

pub const LEAD: &str = include_str!("../roles/lead/AGENTS.md");
pub const SKILL_HEADER: &str = include_str!("../assets/heeler-lead-skill-header.md");

pub fn write_lead(output: &mut impl Write) -> io::Result<()> {
    output.write_all(LEAD.as_bytes())
}

pub fn write_skill(output: &mut impl Write) -> io::Result<()> {
    output.write_all(SKILL_HEADER.as_bytes())?;
    output.write_all(b"\n")?;
    write_lead(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exports_preserve_one_exact_canonical_body_and_manual_gate() {
        let mut lead = Vec::new();
        write_lead(&mut lead).unwrap();
        assert_eq!(lead, LEAD.as_bytes());
        let mut skill = Vec::new();
        write_skill(&mut skill).unwrap();
        assert_eq!(skill, format!("{SKILL_HEADER}\n{LEAD}").as_bytes());
        assert_eq!(skill.windows(lead.len()).filter(|s| *s == lead).count(), 1);
        assert!(SKILL_HEADER.starts_with("---\nname: heeler-lead\n"));
        assert!(SKILL_HEADER.contains("disable-model-invocation: true"));
        assert!(SKILL_HEADER.contains("explicit human request"));
        assert!(!SKILL_HEADER.contains("../../"));
    }

    #[test]
    fn export_propagates_output_errors_without_retries() {
        struct Broken;
        impl Write for Broken {
            fn write(&mut self, _: &[u8]) -> io::Result<usize> {
                Err(io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        assert_eq!(
            write_lead(&mut Broken).unwrap_err().kind(),
            io::ErrorKind::BrokenPipe
        );
        assert_eq!(
            write_skill(&mut Broken).unwrap_err().kind(),
            io::ErrorKind::BrokenPipe
        );
    }
}
