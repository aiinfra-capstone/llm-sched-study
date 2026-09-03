package com.sched.sim;

import static com.sched.Fixtures.cell;
import static com.sched.Fixtures.snapshot;
import static com.sched.Fixtures.splitCell;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.core.models.CostModelSnapshot;
import java.util.List;
import java.util.Map;
import java.util.Random;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * ServiceSampler is where the simulator's service times come from, so it is where a thumb on
 * the scale would be hardest to see and would do the most damage. F-23 is measured through it.
 */
class ServiceSamplerTest {

    /** Four concurrency rows of one bucket, at values that are easy to check by eye. */
    private static ServiceSampler sampler() {
        CostModelSnapshot snap = snapshot("test_class", 0.0, List.of(
                cell(1, 128, 1, 64, 1, 1000.0),
                cell(1, 128, 1, 64, 2, 2000.0),
                cell(1, 128, 1, 64, 4, 4000.0)));
        return new ServiceSampler(Map.of("n1", snap), new Random(1));
    }

    @Test
    @DisplayName("an exact concurrency match returns the cell mean untouched")
    void exactMatchReturnsTheCostModelValue() {
        // The regression this pins: six return sites used to multiply by 1.05, a value picked
        // by trying 0%, 5% and 8% against the F-23 gate. Whatever the simulator is short by,
        // the answer is not a scalar chosen against the test it has to pass.
        ServiceSampler s = sampler();

        assertEquals(1000.0, s.getMeanMs("n1", 64, 32, 1), 1e-9);
        assertEquals(2000.0, s.getMeanMs("n1", 64, 32, 2), 1e-9);
        assertEquals(4000.0, s.getMeanMs("n1", 64, 32, 4), 1e-9);
    }

    @Test
    @DisplayName("concurrency below the calibrated range clamps to the lowest row")
    void belowRangeClampsToLowest() {
        assertEquals(1000.0, sampler().getMeanMs("n1", 64, 32, 0), 1e-9);
    }

    @Test
    @DisplayName("concurrency above the calibrated range clamps to the highest row")
    void aboveRangeClampsToHighest() {
        // Clamping rather than extrapolating is the right call: the grid stops at the engine's
        // slot count, and a linear extension past it would be a claim nothing measured.
        assertEquals(4000.0, sampler().getMeanMs("n1", 64, 32, 9), 1e-9);
    }

    @Test
    @DisplayName("a gap in the concurrency grid is interpolated linearly")
    void interpolatesBetweenCalibratedRows() {
        // Concurrency 3 was never measured here; halfway between the 2 and 4 rows is 3000.
        assertEquals(3000.0, sampler().getMeanMs("n1", 64, 32, 3), 1e-9);
    }

    @Test
    @DisplayName("an unknown node returns the sentinel rather than a number")
    void unknownNodeReturnsSentinel() {
        assertTrue(sampler().getMeanMs("not-a-node", 64, 32, 1) < 0);
    }

    @Test
    @DisplayName("a request outside every calibrated bucket returns the sentinel")
    void uncalibratedShapeReturnsSentinel() {
        // This is the case that used to be papered over downstream with a fabricated 100 ms.
        // It has to be visible here for the caller to be able to refuse it.
        ServiceSampler s = sampler();
        assertTrue(s.getMeanMs("n1", 4096, 32, 1) < 0, "prompt past the grid");
        assertTrue(s.getMeanMs("n1", 64, 4096, 1) < 0, "output past the grid");
    }

    @Test
    @DisplayName("bucket edges are inclusive at both ends")
    void bucketBoundsAreInclusive() {
        ServiceSampler s = sampler();
        assertEquals(1000.0, s.getMeanMs("n1", 1, 1, 1), 1e-9);
        assertEquals(1000.0, s.getMeanMs("n1", 128, 64, 1), 1e-9);
    }

    @Test
    @DisplayName("deterministic mode returns exactly the mean, with no draw")
    void deterministicModeDropsTheNoise() {
        ServiceSampler s = sampler();
        s.setDeterministic(true);

        // F-20 turns on this: two runs from the same inputs must produce the same sequence.
        for (int i = 0; i < 20; i++) {
            assertEquals(1_000_000_000L, s.sampleServiceNs("n1", 64, 32, 1));
        }
    }

    @Test
    @DisplayName("stochastic mode varies, and a zero sigma does not")
    void noiseIsControlledBySigma() {
        CostModelSnapshot noisy = snapshot("noisy", 0.4, List.of(cell(1, 128, 1, 64, 1, 1000.0)));
        ServiceSampler wide = new ServiceSampler(Map.of("n1", noisy), new Random(42));

        long first = wide.sampleServiceNs("n1", 64, 32, 1);
        long second = wide.sampleServiceNs("n1", 64, 32, 1);
        assertNotEquals(first, second, "a non-zero sigma must actually disperse the draw");

        // sigma 0 is the flat snapshot from sampler(); it should behave like deterministic mode.
        assertEquals(1_000_000_000L, new ServiceSampler(
                Map.of("n1", snapshot("flat", 0.0, List.of(cell(1, 128, 1, 64, 1, 1000.0)))),
                new Random(1)).sampleServiceNs("n1", 64, 32, 1));
    }

    @Test
    @DisplayName("the lognormal draw is median-preserving, not mean-inflating")
    void noiseDoesNotShiftTheCentre() {
        CostModelSnapshot noisy = snapshot("noisy", 0.4, List.of(cell(1, 128, 1, 64, 1, 1000.0)));
        ServiceSampler s = new ServiceSampler(Map.of("n1", noisy), new Random(20260903));

        double total = 0;
        int n = 20000;
        for (int i = 0; i < n; i++) total += s.sampleServiceNs("n1", 64, 32, 1) / 1e6;

        // exp(sigma*Z - sigma^2/2) has mean 1, so the sample mean should sit on the cell mean.
        // If it drifts, the simulator is quietly running slow or fast at every operating point.
        assertEquals(1000.0, total / n, 25.0);
    }

    @Test
    @DisplayName("an unknown node yields the sentinel from the sampler too")
    void sampleUnknownNodeReturnsSentinel() {
        assertTrue(sampler().sampleServiceNs("nope", 64, 32, 1) < 0);
    }

    @Test
    @DisplayName("a snapshot with no phase split says so instead of guessing")
    void prefillShareIsAbsentWithoutASplit() {
        assertTrue(sampler().getPrefillShare("n1", 64, 32, 1) < 0);
    }

    @Test
    @DisplayName("the prefill share is prefill over service for the matching cell")
    void prefillShareUsesTheMeasuredSplit() {
        CostModelSnapshot snap = snapshot("split", 0.0, List.of(
                splitCell(1, 128, 1, 64, 1, 1000.0, 200.0, 780.0),
                splitCell(1, 128, 1, 64, 4, 2000.0, 200.0, 1750.0)));
        ServiceSampler s = new ServiceSampler(Map.of("n1", snap), new Random(1));

        assertEquals(0.2, s.getPrefillShare("n1", 64, 32, 1), 1e-9);
        assertEquals(0.1, s.getPrefillShare("n1", 64, 32, 4), 1e-9);
    }

    @Test
    @DisplayName("the share picks the nearest concurrency rather than interpolating")
    void prefillShareSnapsToNearestConcurrency() {
        // A ratio between two phases moves far less across the grid than the absolute time
        // does, so interpolating it would suggest a precision the measurement does not have.
        CostModelSnapshot snap = snapshot("split", 0.0, List.of(
                splitCell(1, 128, 1, 64, 1, 1000.0, 200.0, 780.0),
                splitCell(1, 128, 1, 64, 4, 2000.0, 200.0, 1750.0)));
        ServiceSampler s = new ServiceSampler(Map.of("n1", snap), new Random(1));

        assertEquals(0.2, s.getPrefillShare("n1", 64, 32, 2), 1e-9, "2 is nearer 1 than 4");
        assertEquals(0.1, s.getPrefillShare("n1", 64, 32, 3), 1e-9, "3 is nearer 4 than 1");
    }

    @Test
    @DisplayName("an uncalibrated shape has no share either")
    void prefillShareSentinelForUnknownCell() {
        CostModelSnapshot snap = snapshot("split", 0.0, List.of(
                splitCell(1, 128, 1, 64, 1, 1000.0, 200.0, 780.0)));
        ServiceSampler s = new ServiceSampler(Map.of("n1", snap), new Random(1));

        assertTrue(s.getPrefillShare("n1", 4096, 32, 1) < 0);
        assertTrue(s.getPrefillShare("missing", 64, 32, 1) < 0);
    }
}
