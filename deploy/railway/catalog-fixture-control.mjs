// Test-only preload. Exposes real catalog owners inside an isolated fixture
// gateway; never imported by the production image or its entrypoint.
import { registerHooks } from 'node:module';
import { isMainThread } from 'node:worker_threads';
import fs from 'node:fs';
import http from 'node:http';

if (isMainThread && process.argv.includes('gateway') && process.env.GENAIE_CATALOG_FIXTURE_CONTROL) {
  const owners = [];
  const captures = [];
  const loaded = [];
  globalThis[Symbol.for('genaie.fixture.catalogOwners')] = owners;
  globalThis[Symbol.for('genaie.fixture.catalogCaptures')] = captures;
  registerHooks({
    load(url, context, next) {
      const result = next(url, context);
      if (result.format !== 'module' || !url.includes('/prepared-model-catalog-worker-')) return result;
      loaded.push(url);
      let source = typeof result.source === 'string' ? result.source : new TextDecoder().decode(result.source);
      if (!source.includes('function createPreparedModelCatalogWorker(params)')) return result;
      const begin = 'return {\n\t\tloadCatalog: async (providerIds, onRecovery) => {';
      const end = '\n\t};\n}\n//#endregion\nexport {';
      const capture = 'const capture = createPluginSourceCaptureRoot(resolveStateDir(env), "openclaw-model-catalog-");';
      for (const anchor of [begin, end, capture]) {
        if (source.split(anchor).length !== 2) throw new Error('fixture catalog-owner instrumentation needs review');
      }
      source = source.replace(begin, 'const fixtureOwner = {\n\t\tloadCatalog: async (providerIds, onRecovery) => {')
        .replace(end, '\n\t};\n globalThis[Symbol.for("genaie.fixture.catalogOwners")].push(fixtureOwner);\n return fixtureOwner;\n}\n//#endregion\nexport {')
        .replace(capture, capture + '\n globalThis[Symbol.for("genaie.fixture.catalogCaptures")].push(capture.directory);');
      return { ...result, source };
    },
  });
  const server = http.createServer(async (request, response) => {
    try {
      const command = request.url.slice(1);
      const current = globalThis[Symbol.for('openclaw.gatewayModelCatalogPool')]?.current;
      if (command === 'snapshot') {
        response.end(JSON.stringify({ owners: owners.length, loaded, pid: process.pid,
          pool: current?.pool.getSnapshot(), captures: captures.map((directory) => ({
            exists: fs.existsSync(directory), directory,
          })) }));
        return;
      }
      const owner = owners.at(-1);
      if (!owner) throw new Error('normal gateway bootstrap has not created a catalog owner');
      if (command === 'catalog') {
        const value = await owner.loadCatalog(['openai']);
        response.end(JSON.stringify({ fixtureModel: JSON.stringify(value).includes('fixture-model') }));
      } else if (command === 'auth') {
        const value = await owner.loadAuth({ providerIds: ['openai'] });
        response.end(JSON.stringify({ fixtureCredential: JSON.stringify(value).includes('fixture-model-key-v1') }));
      } else {
        response.statusCode = 404;
        response.end('{}');
      }
    } catch (error) {
      response.statusCode = 500;
      response.end(JSON.stringify({ error: error.message }));
    }
  });
  server.listen(0, '127.0.0.1', () => {
    fs.writeFileSync(process.env.GENAIE_CATALOG_FIXTURE_CONTROL,
      JSON.stringify({ port: server.address().port }));
  });
  server.unref();
}
