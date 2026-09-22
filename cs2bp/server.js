const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const app = express();
const server = http.createServer(app);
const io = new Server(server, {
    cors: { origin: "*", methods: ["GET", "POST"] }
});

// ============================================================
//  v1.2.0：身份令牌与重连（v1.1）+ 观战模式、服务端掷骰、赛制（BO1/3/5）
//  room = { id, hostPlayerId, createdAt, lastActive, emptySince, members, state }
//  member = { playerId, username, role: host|player|spectator, team, socketId, online }
// ============================================================
const rooms = {};
const MAX_ROOMS = 200;
const MAX_SPECTATORS = 6;                     // 观战席上限（性能优先）
const MAX_MEMBERS = 8;
const EMPTY_ROOM_TTL_MS = 30 * 60 * 1000;
const ABSOLUTE_TTL_MS = 12 * 60 * 60 * 1000;

// v1.2.3：常用名单（持久化到 data/roster.json，新房间自动带入）
const ROSTER_FILE = process.env.UC_ROSTER_FILE || path.join(__dirname, 'data', 'roster.json');
function loadCommonRoster() {
    try {
        const data = JSON.parse(fs.readFileSync(ROSTER_FILE, 'utf8'));
        if (Array.isArray(data && data.players)) {
            return data.players.map(x => String(x == null ? '' : x).trim()).filter(Boolean).slice(0, 50);
        }
    } catch (e) {}
    return [];
}
function saveCommonRoster(players, by) {
    try {
        const dir = path.dirname(ROSTER_FILE);
        if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
        fs.writeFileSync(ROSTER_FILE, JSON.stringify({ players, savedAt: Date.now(), savedBy: by }, null, 2));
        return true;
    } catch (e) { console.log('saveCommonRoster 失败:', e.message); return false; }
}

function generateRoomId() { return crypto.randomBytes(3).toString('hex').toUpperCase(); }
function sanitizeName(s) { return String(s == null ? '' : s).trim().slice(0, 24); }

function defaultState(username) {
    return {
        currentPhase: 'player-selection',
        matchFormat: 'BO3',            // 'BO1' | 'BO3' | 'BO5'
        diceRolled: false,
        diceResults: { team1: 0, team2: 0 },
        selectionOrder: null,
        lastToPick: null,
        firstHandTeam: null,
        secondHandTeam: null,
        currentSelector: null,
        stepStartedAt: null,
        bpStartedAt: null,
        bpEndedAt: null,
        history: [],
        selectedPlayers: {
            team1: [username + " (队长)"],
            team2: ["等待队员加入..."]
        },
        availablePlayers: loadCommonRoster(),
        teamNames: { team1: "队伍A", team2: "队伍B" },
        mapPool: [
            "de_dust2", "de_inferno", "de_nuke",
            "de_overpass", "de_ancient", "de_train", "de_mirage"
        ],
        selectedPlayer: null,
        currentStep: 0,
        bannedMaps: [],
        selectedMaps: { map1: null, map2: null, map3: null, map4: null, map5: null },
        campChoices: {
            map1: { team: null, choice: null },
            map2: { team: null, choice: null },
            map3: { team: null, choice: null },
            map4: { team: null, choice: null }
        },
        selectedMap: null
    };
}

function serializeUsers(room) {
    const out = {};
    Object.values(room.members).forEach(m => {
        out[m.playerId] = { playerId: m.playerId, username: m.username, role: m.role, team: m.team || null, online: !!m.online };
    });
    return out;
}
function broadcastUsers(room) { io.to(room.id).emit('updateUsers', serializeUsers(room)); }
function emitState(room) { io.to(room.id).emit('updateState', room.state); }

function detachOldSocket(room, member, newSocket) {
    if (member.socketId && member.socketId !== newSocket.id) {
        const old = io.sockets.sockets.get(member.socketId);
        if (old) {
            old.emit('kicked', { message: '该身份已在新的窗口/设备重新连接，此页面已下线' });
            old.leave(room.id);
            old.data.roomId = null;
            old.data.playerId = null;
        }
    }
}
function attachMember(room, playerId, username, role, team, socket) {
    room.members[playerId] = { playerId, username, role, team: team || null, socketId: socket.id, online: true };
    if (role === 'host') room.hostPlayerId = playerId;
    socket.join(room.id);
    socket.data.roomId = room.id;
    socket.data.playerId = playerId;
    socket.data.username = username;
    socket.data.team = team || null;
}

setInterval(() => {
    const now = Date.now();
    Object.keys(rooms).forEach(id => {
        const r = rooms[id];
        if (r.emptySince && now - r.emptySince > EMPTY_ROOM_TTL_MS) { delete rooms[id]; return; }
        if (now - r.createdAt > ABSOLUTE_TTL_MS) { delete rooms[id]; }
    });
}, 5 * 60 * 1000).unref();

app.use(express.static('public'));
app.get('/healthz', (req, res) => res.json({ ok: true, rooms: Object.keys(rooms).length }));

io.on('connection', (socket) => {
    console.log('新客户端连接:', socket.id);

    // 创建房间
    socket.on('createRoom', (payload) => {
        const username = sanitizeName(typeof payload === 'string' ? payload : (payload && payload.username));
        const playerId = (payload && payload.playerId) || ('anon-' + socket.id);
        if (!username) { socket.emit('error', '请输入你的昵称'); return; }
        if (socket.data.roomId && rooms[socket.data.roomId]) { socket.emit('error', '你已在房间中，刷新页面可重新连接'); return; }
        if (Object.keys(rooms).length >= MAX_ROOMS) { socket.emit('error', '房间数量已达上限，请稍后再试'); return; }

        const roomId = generateRoomId();
        const room = { id: roomId, hostPlayerId: playerId, createdAt: Date.now(), lastActive: Date.now(), emptySince: null, members: {}, state: defaultState(username) };
        rooms[roomId] = room;
        attachMember(room, playerId, username, 'host', 'team1', socket);
        socket.emit('roomCreated', { roomId, team: 'team1', role: 'host' });
        broadcastUsers(room); emitState(room);
    });

    // 加入房间 / 重连 / 观战
    socket.on('joinRoom', (payload) => {
        const p = payload || {};
        const roomId = String(p.roomId || '').trim().toUpperCase();
        let username = sanitizeName(p.username);
        const playerId = p.playerId || ('anon-' + socket.id);
        const wantSpectate = !!p.spectator;

        const room = rooms[roomId];
        if (!room) { socket.emit('error', '房间不存在（或已被回收）'); return; }

        const existing = room.members[playerId];
        if (existing) {
            // 重连：保留身份（含观战身份）
            detachOldSocket(room, existing, socket);
            attachMember(room, playerId, existing.username, existing.role, existing.team, socket);
            room.lastActive = Date.now(); room.emptySince = null;
            socket.emit('roomJoined', { roomId, team: existing.team, username: existing.username, rejoin: true, role: existing.role, spectator: existing.role === 'spectator' });
            socket.to(roomId).emit('userRejoined', { username: existing.username, team: existing.team });
            broadcastUsers(room); emitState(room);
            return;
        }

        if (socket.data.roomId && rooms[socket.data.roomId]) { socket.emit('error', '你已在其它房间中'); return; }

        const players = Object.values(room.members).filter(m => m.role !== 'spectator');
        const specs = Object.values(room.members).filter(m => m.role === 'spectator');

        if (wantSpectate) {
            if (specs.length >= MAX_SPECTATORS) { socket.emit('error', '观战席已满'); return; }
            if (Object.keys(room.members).length >= MAX_MEMBERS) { socket.emit('error', '房间人数已满'); return; }
            if (!username) username = '观众' + Math.floor(1000 + Math.random() * 9000);
            attachMember(room, playerId, username, 'spectator', null, socket);
            room.lastActive = Date.now(); room.emptySince = null;
            socket.emit('roomJoined', { roomId, team: null, username, role: 'spectator', spectator: true });
            socket.to(roomId).emit('userJoined', { username, team: null, spectator: true });
            broadcastUsers(room); emitState(room);
            console.log(`观战加入: ${username} @ ${roomId}`);
            return;
        }

        // 新玩家
        if (players.length >= 2) { socket.emit('error', '房间已满：另一名玩家掉线中，请让其本人用原设备/浏览器重新加入'); return; }
        if (!username) { socket.emit('error', '请输入你的昵称'); return; }
        attachMember(room, playerId, username, 'player', 'team2', socket);
        room.state.selectedPlayers.team2 = [username + " (队长)"];
        room.lastActive = Date.now(); room.emptySince = null;
        socket.emit('roomJoined', { roomId, team: 'team2', username, role: 'player' });
        socket.to(roomId).emit('userJoined', { username, team: 'team2' });
        broadcastUsers(room); emitState(room);
    });

    socket.on('requestState', () => {
        const room = rooms[socket.data.roomId];
        const m = room && room.members[socket.data.playerId];
        if (!room || !m || m.socketId !== socket.id) return;
        socket.emit('updateState', room.state);
        broadcastUsers(room);
    });

    // 发送消息（观战可参与聊天）
    socket.on('sendMessage', (message) => {
        const room = rooms[socket.data.roomId];
        const m = room && room.members[socket.data.playerId];
        if (room && m) {
            io.to(room.id).emit('newMessage', { username: m.username, message: String(message == null ? '' : message).slice(0, 500) });
        }
    });

    // v1.2.3：保存常用名单
    socket.on('saveRoster', (payload) => {
        const room = rooms[socket.data.roomId];
        const m = room && room.members[socket.data.playerId];
        if (!room || !m || m.socketId !== socket.id) return;
        if (m.role === 'spectator') return;
        let players = [];
        if (payload && Array.isArray(payload.players)) {
            players = payload.players.map(x => String(x == null ? '' : x).trim()).filter(Boolean);
            players = Array.from(new Set(players)).slice(0, 50);
        }
        if (!players.length) { socket.emit('error', '名单为空，未保存'); return; }
        if (saveCommonRoster(players, m.username)) {
            io.to(room.id).emit('rosterSaved', { by: m.username, count: players.length, players });
            console.log('常用名单已保存（' + players.length + ' 名, by ' + m.username + '）');
        }
    });

    // v1.2：服务端掷骰（防作弊、留痕）
    socket.on('rollDice', () => {
        const room = rooms[socket.data.roomId];
        const m = room && room.members[socket.data.playerId];
        if (!room || !m || m.socketId !== socket.id) return;
        if (m.role === 'spectator') return;
        if (room.state.diceRolled) return;
        const hostMember = Object.values(room.members).find(x => x.role === 'host');
        const hostOnline = hostMember && hostMember.online;
        if (!(m.role === 'host' || !hostOnline)) return;
        const seatedPlayers = Object.values(room.members).filter(x => x.role !== 'spectator');
        if (seatedPlayers.length < 2) return;

        let a, b;
        do { a = crypto.randomInt(1, 7); b = crypto.randomInt(1, 7); } while (a === b);
        room.state.diceRolled = true;
        room.state.diceResults = { team1: a, team2: b };
        room.state.selectionOrder = a > b ? 'team1' : 'team2';
        room.state.history = room.state.history || [];
        room.state.history.push(`🎲 掷骰：队伍A ${a} - ${b} 队伍B → ${a > b ? '队伍A' : '队伍B'} 获得选人先手`);
        room.lastActive = Date.now();
        io.to(room.id).emit('updateState', room.state);
    });

    // 状态同步（仅玩家/房主可写，观战只读）
    socket.on('updateState', (newState) => {
        const room = rooms[socket.data.roomId];
        const m = room && room.members[socket.data.playerId];
        if (!room || !m || m.socketId !== socket.id) return;
        if (m.role === 'spectator') return;
        if (!newState || typeof newState !== 'object') return;
        room.state = newState;
        room.lastActive = Date.now();
        io.to(room.id).emit('updateState', newState);
    });

    socket.on('disconnect', () => {
        console.log('客户端断开连接:', socket.id);
        const room = rooms[socket.data.roomId];
        if (!room) return;
        const m = room.members[socket.data.playerId];
        if (m && m.socketId === socket.id) {
            m.online = false; m.socketId = null;
            io.to(room.id).emit('userOffline', { username: m.username, team: m.team || null });
            broadcastUsers(room);
        }
        room.lastActive = Date.now();
        if (Object.values(room.members).every(x => !x.online)) room.emptySince = Date.now();
    });
});

const PORT = process.env.PORT || 4900;
server.listen(PORT, () => { console.log(`服务器运行在 http://localhost:${PORT}`); });
