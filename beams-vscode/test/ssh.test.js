const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { ensureBeamSshConfig } = require('../out/ssh');

test('repairs only the matching legacy Beam SSH blocks', async () => {
    const originalHome = process.env.HOME;
    const originalUserProfile = process.env.USERPROFILE;
    const testHome = fs.mkdtempSync(path.join(os.tmpdir(), 'beam-vs-code-'));
    const configPath = path.join(testHome, '.ssh', 'config');
    const backupPath = `${configPath}.teleport-beams.bak`;
    fs.mkdirSync(path.dirname(configPath));
    const originalConfig = [
        'Host unrelated',
        '    HostName api.example.beams.run',
        '',
        '# BEGIN Teleport Beams',
        'Host *.example.beams.run !example.beams.run',
        '    ProxyCommand tsh proxy ssh --proxy=example.beams.run:443 %h',
        '',
        'Host vscode--beam-1.example.beams.sh',
        '    HostName beam-1.example.beams.run',
        '# END Teleport Beams',
        '',
    ].join('\n');
    fs.writeFileSync(configPath, originalConfig);

    try {
        process.env.HOME = testHome;
        process.env.USERPROFILE = testHome;
        const host = await ensureBeamSshConfig('beam-1', 'example.beams.sh');
        const config = fs.readFileSync(configPath, 'utf8');

        assert.equal(host, 'vscode--beam-1.example.beams.sh');
        assert.match(config, /Host \*\.example\.beams\.sh !example\.beams\.sh/);
        assert.match(config, /ProxyCommand .*--proxy=example\.beams\.sh:443/);
        assert.match(config, /HostName beam-1\.example\.beams\.sh/);
        assert.match(config, /HostName api\.example\.beams\.run/);
        assert.equal(fs.readFileSync(backupPath, 'utf8'), originalConfig);
        if (process.platform !== 'win32') {
            assert.equal(fs.statSync(configPath).mode & 0o777, 0o600);
            assert.equal(fs.statSync(backupPath).mode & 0o777, 0o600);
        }
        assert.deepEqual(fs.readdirSync(path.dirname(configPath)).sort(), [
            'config',
            'config.teleport-beams.bak',
        ]);
    } finally {
        if (originalHome === undefined) {
            delete process.env.HOME;
        } else {
            process.env.HOME = originalHome;
        }
        if (originalUserProfile === undefined) {
            delete process.env.USERPROFILE;
        } else {
            process.env.USERPROFILE = originalUserProfile;
        }
        fs.rmSync(testHome, { recursive: true, force: true });
    }
});

test('hoists a Beam entry above the tsh wildcard so /dev/null known_hosts wins', async () => {
    const originalHome = process.env.HOME;
    const originalUserProfile = process.env.USERPROFILE;
    const testHome = fs.mkdtempSync(path.join(os.tmpdir(), 'beam-vs-code-'));
    const configPath = path.join(testHome, '.ssh', 'config');
    fs.mkdirSync(path.dirname(configPath));
    fs.writeFileSync(configPath, [
        '# BEGIN Teleport Beams',
        'Host vscode--other.example.beams.sh',
        '    HostName other.example.beams.sh',
        '    UserKnownHostsFile /dev/null',
        '',
        '# Begin generated Teleport configuration for example.beams.sh by tsh',
        '',
        'Host *.example.beams.sh example.beams.sh',
        '    UserKnownHostsFile "/home/u/.tsh/known_hosts"',
        '',
        'Host *.example.beams.sh !example.beams.sh',
        '    ProxyCommand tsh proxy ssh %h',
        '',
        '# End generated Teleport configuration',
        '',
        '',
        'Host vscode--beam-1.example.beams.sh',
        '    HostName beam-1.example.beams.sh',
        '    UserKnownHostsFile /dev/null',
        '    RemoteCommand bash',
        '# END Teleport Beams',
        '',
    ].join('\n'));

    try {
        process.env.HOME = testHome;
        process.env.USERPROFILE = testHome;
        const check = async (beamId) => {
            await ensureBeamSshConfig(beamId, 'example.beams.sh');
            const config = fs.readFileSync(configPath, 'utf8');
            const beamIdx = config.indexOf(`Host vscode--${beamId}.example.beams.sh`);
            assert.ok(beamIdx !== -1);
            assert.ok(beamIdx < config.indexOf('# Begin generated Teleport'), config);
            assert.ok(config.indexOf('# END Teleport Beams') > config.indexOf('# End generated Teleport'));
            return config;
        };
        const repaired = await check('beam-1');
        assert.match(repaired, /HostName beam-1\.example\.beams\.sh\n    UserKnownHostsFile \/dev\/null\n    RemoteCommand bash\n\n# Begin/);
        await check('beam-2');
    } finally {
        if (originalHome === undefined) {
            delete process.env.HOME;
        } else {
            process.env.HOME = originalHome;
        }
        if (originalUserProfile === undefined) {
            delete process.env.USERPROFILE;
        } else {
            process.env.USERPROFILE = originalUserProfile;
        }
        fs.rmSync(testHome, { recursive: true, force: true });
    }
});
