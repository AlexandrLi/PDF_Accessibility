/**
 * Heuristics for figure alt text quality (mirrors scripts/lib/figure_alt_quality.py).
 */

const TRUNCATED_LIST_ITEM =
    /(?:stages?|steps?|phases?|parts?|points?|items?|types?|examples?|diagrams?)\s*:\s*\d+\s*$/i;

const TRUNCATED_COLON_DIGIT = /:\s*\d+\s*$/;

const RETRYABLE_REASONS = new Set([
    'truncated_list_enumeration',
    'truncated_colon_digit',
]);

const MAX_ALT_TEXT_ATTEMPTS = 3;

function classifyFigureAlt(altText) {
    const text = (altText || '').trim();
    if (!text) {
        return [];
    }

    const reasons = [];

    if (TRUNCATED_LIST_ITEM.test(text)) {
        reasons.push('truncated_list_enumeration');
    } else if (TRUNCATED_COLON_DIGIT.test(text) && text.length < 200) {
        reasons.push('truncated_colon_digit');
    }

    return reasons;
}

function isSuspiciousFigureAlt(altText) {
    return classifyFigureAlt(altText).length > 0;
}

function isRetryableSuspiciousAlt(altText) {
    const reasons = classifyFigureAlt(altText);
    return (
        reasons.length > 0 && reasons.every((reason) => RETRYABLE_REASONS.has(reason))
    );
}

function stripMarkdownJsonFence(text) {
    const trimmed = text.trim();
    const fenced = trimmed.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i);
    return fenced ? fenced[1].trim() : trimmed;
}

function parseAltTextFromResponse(responseText, expectedId) {
    const cleaned = stripMarkdownJsonFence(responseText);
    try {
        return JSON.parse(cleaned);
    } catch {
        const jsonMatch = cleaned.match(/\{[\s\S]*\}/);
        if (jsonMatch) {
            try {
                return JSON.parse(jsonMatch[0]);
            } catch {
                // fall through to regex extraction
            }
        }

        const keyPattern = new RegExp(
            `"${String(expectedId)}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`
        );
        const match = cleaned.match(keyPattern);
        if (match) {
            return { [String(expectedId)]: match[1].replace(/\\"/g, '"') };
        }

        throw new Error('Could not parse alt text JSON from model response');
    }
}

module.exports = {
    MAX_ALT_TEXT_ATTEMPTS,
    classifyFigureAlt,
    isSuspiciousFigureAlt,
    isRetryableSuspiciousAlt,
    parseAltTextFromResponse,
    RETRYABLE_REASONS,
};
