// wMaktab statistics engine (C++17)
//
// Reads "key value" pairs from standard input (one pair per line) and prints
// one JSON object with descriptive statistics for every key.
//
//   build:  g++ -O2 -std=c++17 -o bin/wmaktab_stats statistics.cpp
//   usage:  printf 'math 5\nmath 4\nmath 3\n' | ./bin/wmaktab_stats
//
// The Flask application (stats_bridge.py) compiles and calls this program
// automatically; grades are 1..5 but any numeric value is accepted.
//
// Output per key:
//   count, mean, median, min, max, stddev (sample), mode,
//   mark          - mean rounded half-up to the 1..5 school scale
//   quality_rate  - % of values that round to 4 or 5
//   pass_rate     - % of values that round to 3 or higher
//   dist          - how many values round to each of 1..5

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

using namespace std;

static string jsonEscape(const string &s) {
    string out;
    for (char c : s) {
        if (c == '"' || c == '\\') {
            out += '\\';
            out += c;
        } else if (static_cast<unsigned char>(c) < 0x20) {
            out += ' ';
        } else {
            out += c;
        }
    }
    return out;
}

// Round half up (so 3.5 -> 4), then clamp to the 1..5 scale.
static int toScale(double x) {
    int r = static_cast<int>(floor(x + 0.5));
    return max(1, min(5, r));
}

static double median(vector<double> v) {
    sort(v.begin(), v.end());
    size_t n = v.size();
    if (n % 2 == 1) return v[n / 2];
    return (v[n / 2 - 1] + v[n / 2]) / 2.0;
}

static void printGroup(const string &key, const vector<double> &vals) {
    const size_t n = vals.size();
    double sum = 0.0;
    double lo = vals[0];
    double hi = vals[0];
    int dist[6] = {0, 0, 0, 0, 0, 0};
    int quality = 0;
    int passed = 0;

    for (double x : vals) {
        sum += x;
        lo = min(lo, x);
        hi = max(hi, x);
        dist[toScale(x)]++;
        if (floor(x + 0.5) >= 4) quality++;
        if (floor(x + 0.5) >= 3) passed++;
    }

    const double mean = sum / static_cast<double>(n);
    double sq = 0.0;
    for (double x : vals) sq += (x - mean) * (x - mean);
    const double stddev = n > 1 ? sqrt(sq / static_cast<double>(n - 1)) : 0.0;

    int mode = 1;
    for (int k = 1; k <= 5; k++) {
        if (dist[k] > dist[mode]) mode = k;
    }

    cout << "\"" << jsonEscape(key) << "\":{";
    cout << "\"count\":" << n << ",";
    cout << "\"mean\":" << mean << ",";
    cout << "\"median\":" << median(vals) << ",";
    cout << "\"min\":" << lo << ",";
    cout << "\"max\":" << hi << ",";
    cout << "\"stddev\":" << stddev << ",";
    cout << "\"mode\":" << mode << ",";
    cout << "\"mark\":" << toScale(mean) << ",";
    cout << "\"quality_rate\":" << 100.0 * quality / static_cast<double>(n) << ",";
    cout << "\"pass_rate\":" << 100.0 * passed / static_cast<double>(n) << ",";
    cout << "\"dist\":{";
    for (int k = 1; k <= 5; k++) {
        cout << "\"" << k << "\":" << dist[k] << (k < 5 ? "," : "");
    }
    cout << "}}";
}

int main(int argc, char **argv) {
    ios::sync_with_stdio(false);

    if (argc > 1 && string(argv[1]) == "--version") {
        cout << "wmaktab-stats 1.0\n";
        return 0;
    }

    map<string, vector<double>> groups;
    string line;
    while (getline(cin, line)) {
        istringstream ss(line);
        string key;
        double value;
        if (!(ss >> key >> value)) continue;  // skip blank or malformed lines
        if (!isfinite(value)) continue;
        groups[key].push_back(value);
    }

    cout << fixed << setprecision(2);
    cout << "{";
    bool first = true;
    for (const auto &entry : groups) {
        if (entry.second.empty()) continue;
        if (!first) cout << ",";
        first = false;
        printGroup(entry.first, entry.second);
    }
    cout << "}\n";
    return 0;
}
