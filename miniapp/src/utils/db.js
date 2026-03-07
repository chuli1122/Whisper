const DB_NAME = "WhisperMiniDB";
const DB_VERSION = 1;
const STORE = "avatars";

const openDB = () =>
  new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onerror = () => reject(req.error);
    req.onsuccess = () => resolve(req.result);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: "key" });
      }
    };
  });

export const saveAvatar = async (key, base64) => {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction([STORE], "readwrite");
    tx.objectStore(STORE).put({ key, base64 });
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
};

export const getAvatar = async (key) => {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction([STORE], "readonly");
    const req = tx.objectStore(STORE).get(key);
    req.onsuccess = () => resolve(req.result?.base64 || null);
    req.onerror = () => reject(req.error);
  });
};

export const deleteAvatar = async (key) => {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction([STORE], "readwrite");
    tx.objectStore(STORE).delete(key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
};
