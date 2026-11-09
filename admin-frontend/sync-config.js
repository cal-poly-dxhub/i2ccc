#!/usr/bin/env node

// Writes admin-frontend/.env from the deployed stack's CloudFormation outputs,
// for local development (pnpm dev, port 5174). Needs AWS credentials for the
// account the stack is in. stack_name and aws_region come from ../config.yaml.
//
// Local login only works on a dev-mode stack (dev_user_email set), the only
// mode that registers http://localhost:5174 as a Cognito callback URL.

import fs from 'fs';
import path from 'path';
import { execFileSync } from 'child_process';
import { fileURLToPath } from 'url';

const dir = path.dirname(fileURLToPath(import.meta.url));
const configText = fs.readFileSync(path.join(dir, '..', 'config.yaml'), 'utf8');
const readKey = (key, fallback) => {
  const m = configText.match(new RegExp(`^${key}:\\s*["']?([^"'\\s#]+)`, 'm'));
  return m ? m[1] : fallback;
};
const stackName = readKey('stack_name', 'RagChatbotStack');
const region = readKey('aws_region', 'us-west-2');

try {
  const raw = execFileSync(
    'aws',
    ['cloudformation', 'describe-stacks', '--stack-name', stackName, '--region', region,
     '--query', 'Stacks[0].Outputs', '--output', 'json'],
    { encoding: 'utf8' },
  );
  const outputs = JSON.parse(raw);

  // Output keys carry a construct prefix and an 8-character hash suffix
  const get = (name) => {
    const hit = outputs.find((o) => o.OutputKey.replace(/[0-9A-F]{8}$/, '').endsWith(name));
    return hit ? hit.OutputValue : '';
  };

  const domain = get('CognitoHostedUiDomain');
  const clientId = get('CognitoAppClientId');
  if (!domain || !clientId) throw new Error(`Cognito outputs not found on stack ${stackName}`);

  const origin = 'http://localhost:5174';
  const env = [
    `VITE_COGNITO_DOMAIN=${domain}`,
    `VITE_COGNITO_CLIENT_ID=${clientId}`,
    `VITE_COGNITO_REDIRECT_URI=${origin}`,
    `VITE_COGNITO_LOGOUT_URI=${origin}`,
    // Empty on dev stacks, which makes the hosted UI show the password form
    `VITE_COGNITO_IDP_NAME=${get('CognitoSamlIdpName')}`,
  ];
  fs.writeFileSync(path.join(dir, '.env'), env.join('\n') + '\n');
  console.log(`✓ .env written from stack ${stackName} (${region})`);
  console.log(`  Cognito domain: ${domain}`);
} catch (error) {
  console.error('Error syncing config:', error.message);
  process.exit(1);
}
