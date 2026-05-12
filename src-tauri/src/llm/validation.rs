use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const MAX_MODEL_RETRIES: usize = 2;

const CORRECTIVE_SYSTEM_MESSAGE: &str = "Your previous response could not be used. Reply again with a non-empty, policy-compliant answer. If you need to call tools, emit valid JSON that matches the tool call schema. Do not refuse due to missing capabilities; explain what you can do next or ask for the minimum missing detail.";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ValidationFailureKind {
    InvalidToolJson,
    EmptyAnswer,
    CapabilityRefusal,
    UnsupportedFinalAnswer,
}

impl ValidationFailureKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::InvalidToolJson => "invalid_tool_json",
            Self::EmptyAnswer => "empty_answer",
            Self::CapabilityRefusal => "capability_refusal",
            Self::UnsupportedFinalAnswer => "unsupported_final_answer",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationFailure {
    pub kind: ValidationFailureKind,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelResponse {
    pub content: String,
    pub tool_calls_json: Option<String>,
    pub is_final_answer: bool,
}

impl ModelResponse {
    pub fn text(content: impl Into<String>) -> Self {
        Self {
            content: content.into(),
            tool_calls_json: None,
            is_final_answer: false,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationContext {
    pub final_answers_supported: bool,
}

impl Default for ValidationContext {
    fn default() -> Self {
        Self {
            final_answers_supported: true,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CritiqueRoute {
    pub provider: String,
    pub model: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetryAttempt {
    pub response: ModelResponse,
    pub corrective_system_message: Option<String>,
    pub critique_route: Option<CritiqueRoute>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetryOutcome {
    pub response: ModelResponse,
    pub failures: Vec<ValidationFailure>,
    pub attempts: usize,
}

pub fn corrective_system_message() -> &'static str {
    CORRECTIVE_SYSTEM_MESSAGE
}

pub fn validate_model_response(
    response: &ModelResponse,
    context: &ValidationContext,
) -> Result<(), ValidationFailure> {
    if let Some(tool_json) = response.tool_calls_json.as_deref() {
        validate_tool_call_json(tool_json)?;
    }

    if response.content.trim().is_empty()
        && response
            .tool_calls_json
            .as_deref()
            .unwrap_or("")
            .trim()
            .is_empty()
    {
        return Err(ValidationFailure {
            kind: ValidationFailureKind::EmptyAnswer,
            detail: "model returned no answer text or tool calls".to_string(),
        });
    }

    if is_capability_refusal(&response.content) {
        return Err(ValidationFailure {
            kind: ValidationFailureKind::CapabilityRefusal,
            detail: "model declined because it claimed it lacked capability instead of giving a useful next step".to_string(),
        });
    }

    if response.is_final_answer && !context.final_answers_supported {
        return Err(ValidationFailure {
            kind: ValidationFailureKind::UnsupportedFinalAnswer,
            detail: "model emitted a final answer before this context supports final answers"
                .to_string(),
        });
    }

    Ok(())
}

pub async fn retry_with_validation<F, Fut>(
    mut generate: F,
    context: ValidationContext,
    critique_route: Option<CritiqueRoute>,
) -> RetryOutcome
where
    F: FnMut(RetryAttempt) -> Fut,
    Fut: std::future::Future<Output = ModelResponse>,
{
    let mut failures = Vec::new();
    let mut corrective_system_message = None;
    let mut previous_response = ModelResponse::text("");

    for attempt in 0..=MAX_MODEL_RETRIES {
        let response = generate(RetryAttempt {
            response: previous_response.clone(),
            corrective_system_message: corrective_system_message.clone(),
            critique_route: if corrective_system_message.is_some() {
                critique_route.clone()
            } else {
                None
            },
        })
        .await;

        match validate_model_response(&response, &context) {
            Ok(()) => {
                return RetryOutcome {
                    response,
                    failures,
                    attempts: attempt + 1,
                };
            }
            Err(failure) => {
                previous_response = response;
                failures.push(failure);
                corrective_system_message = Some(CORRECTIVE_SYSTEM_MESSAGE.to_string());
            }
        }
    }

    let response = ModelResponse::text("I could not produce a valid response after retrying. Please clarify the request or try again.");
    RetryOutcome {
        response,
        failures,
        attempts: MAX_MODEL_RETRIES + 1,
    }
}

fn validate_tool_call_json(tool_json: &str) -> Result<(), ValidationFailure> {
    if tool_json.trim().is_empty() {
        return Ok(());
    }

    let parsed: Value = serde_json::from_str(tool_json).map_err(|e| ValidationFailure {
        kind: ValidationFailureKind::InvalidToolJson,
        detail: format!("tool call JSON could not be parsed: {e}"),
    })?;

    match parsed {
        Value::Array(_) | Value::Object(_) => Ok(()),
        _ => Err(ValidationFailure {
            kind: ValidationFailureKind::InvalidToolJson,
            detail: "tool call JSON must be an object or array".to_string(),
        }),
    }
}

fn is_capability_refusal(content: &str) -> bool {
    let normalized = content.to_lowercase();
    let refusal_stems = [
        "i can't",
        "i cannot",
        "i’m unable",
        "i am unable",
        "i'm unable",
        "as an ai",
        "as a language model",
        "i don't have the ability",
        "i do not have the ability",
        "i don't have the capability",
        "i do not have the capability",
        "i cannot access",
        "i can't access",
    ];

    refusal_stems.iter().any(|stem| normalized.contains(stem))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    #[test]
    fn rejects_invalid_json_tool_calls() {
        let response = ModelResponse {
            content: "Calling a tool".to_string(),
            tool_calls_json: Some("{not-json".to_string()),
            is_final_answer: false,
        };

        let failure =
            validate_model_response(&response, &ValidationContext::default()).unwrap_err();
        assert_eq!(failure.kind, ValidationFailureKind::InvalidToolJson);
    }

    #[tokio::test]
    async fn retries_empty_model_output() {
        let calls = Arc::new(Mutex::new(0));
        let calls_for_generator = calls.clone();

        let outcome = retry_with_validation(
            move |_attempt| {
                let calls_for_future = calls_for_generator.clone();
                async move {
                    let mut count = calls_for_future.lock().unwrap();
                    *count += 1;
                    if *count == 1 {
                        ModelResponse::text("")
                    } else {
                        ModelResponse::text("Recovered answer")
                    }
                }
            },
            ValidationContext::default(),
            None,
        )
        .await;

        assert_eq!(outcome.response.content, "Recovered answer");
        assert_eq!(outcome.failures[0].kind, ValidationFailureKind::EmptyAnswer);
        assert_eq!(*calls.lock().unwrap(), 2);
    }

    #[tokio::test]
    async fn corrective_message_recovers_capability_refusal() {
        let seen_correction = Arc::new(Mutex::new(false));
        let seen_correction_for_generator = seen_correction.clone();

        let outcome = retry_with_validation(
            move |attempt| {
                let seen_correction_for_future = seen_correction_for_generator.clone();
                async move {
                    if attempt.corrective_system_message.is_some() {
                        *seen_correction_for_future.lock().unwrap() = true;
                        ModelResponse::text(
                            "I can help by drafting a script and asking for any required path.",
                        )
                    } else {
                        ModelResponse::text("I can't do that because I don't have the capability.")
                    }
                }
            },
            ValidationContext::default(),
            Some(CritiqueRoute {
                provider: "openai".to_string(),
                model: "gpt-4o-mini".to_string(),
            }),
        )
        .await;

        assert_eq!(
            outcome.failures[0].kind,
            ValidationFailureKind::CapabilityRefusal
        );
        assert_eq!(
            outcome.response.content,
            "I can help by drafting a script and asking for any required path."
        );
        assert!(*seen_correction.lock().unwrap());
    }
}
