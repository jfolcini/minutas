import js from '@eslint/js';

export default [
    js.configs.recommended,
    {
        languageOptions: {
            // What GJS and GNOME Shell add to the ES globals.
            globals: {
                console: 'readonly',
                global: 'readonly',
                print: 'readonly',
                printerr: 'readonly',
                TextDecoder: 'readonly',
                TextEncoder: 'readonly',
            },
        },
        rules: {
            eqeqeq: 'error',
            'prefer-const': 'error',
        },
    },
];
