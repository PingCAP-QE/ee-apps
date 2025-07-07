import config from "./config";

// export const FRONTHOST='http://127.0.0.1:8080';
// export const CLIENTID='aee7050a0cf9f4bbd21e';
// export const SECRET='1253a15d669dc7d76b52a5abf670894314615af3';

export const FRONTHOST='http://tibuild.pingcap.net';
export const CLIENTID='1ad7908d133ed11658d6';
export const SECRET='7e50fa628ae20d78c112ff07eaa22baf80825bc4';
export function url(url) {
  return `${config.SERVER_HOST}${url}`;
}
