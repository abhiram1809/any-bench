import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './studio';
import './studio.css';
import '@xyflow/react/dist/style.css';

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
