#!/usr/bin/env node

// Writes frontend/.env from the deployed stack's CloudFormation outputs.
// Needs AWS credentials for the account the stack is in. stack_name and
// aws_region come from ../config.yaml and default to RagChatbotStack / us-west-2
// (the same defaults as ingest_utils/confluence_processor/stack_config.py).

import fs from 'fs';
import path from 'path';
import { load as loadYaml } from 'js-yaml';
import { execFileSync } from 'child_process';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const configPath = path.join(__dirname, '..', 'config.yaml');
const envPath = path.join(__dirname, '.env');

try {
  const config = loadYaml(fs.readFileSync(configPath, 'utf8')) || {};
  const stackName = config.stack_name || 'RagChatbotStack';
  const region = config.aws_region || 'us-west-2';

  const raw = execFileSync(
    'aws',
    ['cloudformation', 'describe-stacks', '--stack-name', stackName, '--region', region,
     '--query', 'Stacks[0].Outputs', '--output', 'json'],
    { encoding: 'utf8' },
  );

  // Output keys carry a construct prefix and an 8-character hash suffix
  const outputs = JSON.parse(raw).filter((o) => o.OutputKey.replace(/[0-9A-F]{8}$/, '').endsWith('ProxyAPIEndpoint'));
  const urls = [...new Set(outputs.map((o) => o.OutputValue))];
  if (urls.length !== 1) {
    throw new Error(`Expected one ProxyAPIEndpoint output on stack ${stackName}, found ${urls.length}`);
  }

  // The browser talks to the proxy API, which adds the API key server-side
  const apiEndpoint = urls[0].replace(/\/$/, '') + '/api/';

  // Preserve other variables already in .env
  let envContent = '';
  if (fs.existsSync(envPath)) {
    envContent = fs.readFileSync(envPath, 'utf8');
  }

  const envLines = envContent.split('\n').filter(line =>
    !line.startsWith('VITE_API_ENDPOINT=') &&
    !line.startsWith('VITE_API_KEY=') &&
    line.trim() !== ''
  );

  envLines.push(`VITE_API_ENDPOINT=${apiEndpoint}`);

  if (!envLines.some(line => line.startsWith('VITE_ENVIRONMENT='))) {
    envLines.push('VITE_ENVIRONMENT=development');
  }

  fs.writeFileSync(envPath, envLines.join('\n') + '\n');

  console.log(`✓ .env file updated from stack ${stackName} (${region})`);
  console.log(`  API Endpoint: ${apiEndpoint}`);

} catch (error) {
  console.error('Error syncing config:', error.message);
  process.exit(1);
}
