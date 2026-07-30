import { Chart, registerables } from 'chart.js';

// Registered once, at module load — every chart component below just imports
// this module (for its side effect) before rendering a react-chartjs-2 chart.
Chart.register(...registerables);
