const assert = require('assert');
const {
    buildSafeFallbackFigureAlt,
    classifyFigureAlt,
    isBedrockContentFilterResponse,
    isRetryableSuspiciousAlt,
    parseAltTextFromResponse,
} = require('./figure_alt_quality');

const truncatedGlut1 =
    'Diagram illustrating the process of glucose transport via GLUT1 uniporter in erythrocytes. It shows four stages: 1';

assert.deepStrictEqual(classifyFigureAlt(truncatedGlut1), [
    'truncated_list_enumeration',
]);
assert.strictEqual(isRetryableSuspiciousAlt(truncatedGlut1), true);

const complete =
    'Diagram of GLUT1 transport showing four stages from extracellular binding through release inside the cell.';
assert.deepStrictEqual(classifyFigureAlt(complete), []);
assert.strictEqual(isRetryableSuspiciousAlt(complete), false);

assert.deepStrictEqual(parseAltTextFromResponse('```json\n{"42": "hello"}\n```', '42'), {
    42: 'hello',
});

assert.strictEqual(
    isBedrockContentFilterResponse('The generated text has been blocked by our content filters.'),
    true
);
assert.strictEqual(
    buildSafeFallbackFigureAlt({ id: 71 }),
    'Figure 71'
);

console.log('figure_alt_quality.test.js: OK');
