/* test/lock-exit.js — 模拟实例锁被占的「秒退」后端：stderr 打印 InstanceLockError 后 exit 2 */
"use strict";

console.error("Traceback (most recent call last):");
console.error("app.instancelock.InstanceLockError: 后端实例锁已被占用（claim: state/.instance_lock，pid=9999，host=127.0.0.1:8001）");
console.error("InstanceLockError: another ME_Agent instance holds the lock");
process.exit(2);
