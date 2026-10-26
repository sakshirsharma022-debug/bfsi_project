import { initializeApp } from "firebase/app";
import { getAuth, GoogleAuthProvider } from "firebase/auth";
import { getFirestore } from "firebase/firestore";

const firebaseConfig = {
  apiKey: "AIzaSyAPGiFxvQ2pNg-OtUnMeKhhjwWRmtu6r0A",
  authDomain: "quiet-terminal-422106.firebaseapp.com",
  projectId: "quiet-terminal-422106",
  storageBucket: "quiet-terminal-422106.firebasestorage.app",
  messagingSenderId: "367653436138",
  appId: "1:367653436138:web:5564da8ee18eae8dae3593",
  measurementId: "G-F4SYTH7XW9"
};

const app = initializeApp(firebaseConfig);

export const auth = getAuth(app);
export const db = getFirestore(app);
export const googleProvider = new GoogleAuthProvider();